from typing import Any, Mapping, Optional, Tuple  # isort:skip
from abc import ABC, abstractmethod
from collections import OrderedDict
import os
from pathlib import Path

import torch
from torch import nn
from torch.utils.data import DistributedSampler

from catalyst.dl import utils
from catalyst.dl.utils.scripts import dump_base_experiment_code
from catalyst.dl.utils.torch import _Criterion, _Model, _Optimizer, _Scheduler
from .callback import Callback, LoggerCallback
from .experiment import Experiment
from .state import RunnerState


class Runner(ABC):
    def __init__(
        self,
        model: nn.Module = None,
        device=None,
    ):
        """
        @TODO: write docs
        """
        # main
        self.model: nn.Module = model
        self.device = device
        self.experiment: Experiment = None
        self.state: RunnerState = None
        self.callbacks: OrderedDict[str, Callback] = None
        self.loggers: OrderedDict[str, LoggerCallback] = None

        # additional
        self._check_run = False

    def _batch2device(self, batch: Mapping[str, Any], device):
        res = utils.any2device(batch, device)
        return res

    def _get_experiment_components(
        self, stage: str = None
    ) -> Tuple[_Model, _Criterion, _Optimizer, _Scheduler, torch.device]:
        """
        Inner method for children's classes for model specific initialization.
        As baseline, checks device support and puts model on it.
        :return:
        """
        utils.set_global_seed(self.experiment.initial_seed)
        model = self.experiment.get_model(stage)
        criterion, optimizer, scheduler = \
            self.experiment.get_experiment_components(model, stage)

        model, criterion, optimizer, scheduler, device = \
            utils.process_components(
                model=model,
                criterion=criterion,
                optimizer=optimizer,
                scheduler=scheduler,
                distributed_params=self.experiment.distributed_params
            )

        return model, criterion, optimizer, scheduler, device

    def _prepare_for_stage(self, stage: str):
        utils.set_global_seed(self.experiment.initial_seed)
        migrating_params = {}
        if self.state is not None:
            migrating_params.update(
                {
                    "step": self.state.step,
                    "epoch": self.state.epoch
                }
            )

        self.model, criterion, optimizer, scheduler, self.device = \
            self._get_experiment_components(stage)

        self.state = RunnerState(
            stage=stage,
            model=self.model,
            device=self.device,
            criterion=criterion,
            optimizer=optimizer,
            scheduler=scheduler,
            **self.experiment.get_state_params(stage),
            **migrating_params
        )
        utils.set_global_seed(self.experiment.initial_seed)

    def _run_event(self, event: str, moment: Optional[str]):
        fn_name = f"on_{event}"
        if moment is not None:
            fn_name = f"{fn_name}_{moment}"

        # before callbacks
        if self.state is not None:
            getattr(self.state, f"{fn_name}_pre")()

        if self.loggers is not None and moment == "start":
            for logger in self.loggers.values():
                getattr(logger, fn_name)(self.state)

        # running callbacks
        if self.callbacks is not None:
            for callback in self.callbacks.values():
                getattr(callback, fn_name)(self.state)

        # after callbacks
        if self.loggers is not None and \
                (moment == "end" or moment is None):  # for on_exception case
            for logger in self.loggers.values():
                getattr(logger, fn_name)(self.state)

        if self.state is not None:
            getattr(self.state, f"{fn_name}_post")()

    @abstractmethod
    def forward(self, batch: Mapping[str, Any]) -> Mapping[str, Any]:
        pass

    def predict_batch(self, batch: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        Run model for a batch of elements
        WARN: You should not override this method. If you need specific model
        call, override forward() method
        Args:
            batch: Key-value batch items
        Returns: model output key-value
        """
        batch = self._batch2device(batch, self.device)
        output = self.forward(batch)
        return output

    def _run_batch(self, batch):
        self.state.step += self.state.batch_size
        batch = self._batch2device(batch, self.device)
        self.state.input = batch
        self.state.timer.stop("_timers/data_time")

        self._run_event("batch", moment="start")
        self.state.timer.start("_timers/model_time")
        self.state.output = self.forward(batch)
        self.state.timer.stop("_timers/model_time")
        self.state.timer.stop("_timers/batch_time")
        self._run_event("batch", moment="end")

    def _run_loader(self, loader):
        self.state.batch_size = (
            loader.batch_sampler.batch_size
            if loader.batch_sampler is not None else loader.batch_size
        )
        self.state.step = (
            self.state.step
            or self.state.epoch * len(loader) * self.state.batch_size
        )
        # @TODO: remove time usage, use it under the hood
        self.state.timer.reset()

        self.state.timer.start("_timers/batch_time")
        self.state.timer.start("_timers/data_time")

        for i, batch in enumerate(loader):
            self._run_batch(batch)

            self.state.timer.reset()
            if self._check_run and i >= 3:
                break

            self.state.timer.start("_timers/batch_time")
            self.state.timer.start("_timers/data_time")

    def _run_epoch(self, loaders):
        # @TODO: better solution with train/inference handling ?
        if not self.state.stage.startswith("infer"):
            assert self.state.valid_loader in loaders.keys(), \
                f"'{self.state.valid_loader}' " \
                f"should be in provided loaders: {list(loaders.keys())}"
        else:
            assert not any(x.startswith("train") for x in loaders.keys()), \
                "for inference no train loader should be passed"

        for loader_name, loader in loaders.items():
            self.state.loader_name = loader_name
            self.state.loader_len = len(loader)
            self.state.need_backward = loader_name.startswith("train")
            utils.maybe_recursive_call(
                self.model,
                "train",
                mode=self.state.need_backward
            )

            if isinstance(loader.sampler, DistributedSampler) \
                    and loader_name.startswith("train"):
                loader.sampler.set_epoch(self.state.stage_epoch)

            utils.set_global_seed(
                self.experiment.initial_seed + self.state.epoch + 1
            )
            self._run_event("loader", moment="start")
            with torch.set_grad_enabled(self.state.need_backward):
                self._run_loader(loader)
            self._run_event("loader", moment="end")

    def _run_stage(self, stage: str):
        self._prepare_for_stage(stage)
        loaders = self.experiment.get_loaders(stage)
        callbacks = self.experiment.get_callbacks(stage)
        loggers = utils.process_callbacks(
            OrderedDict([
                (k, v) for k, v in callbacks.items()
                if isinstance(v, LoggerCallback)
            ])
        )
        callbacks = utils.process_callbacks(
            OrderedDict([
                (k, v) for k, v in callbacks.items()
                if not isinstance(v, LoggerCallback)
            ])
        )
        self.state.loggers = loggers
        self.loggers = loggers
        self.callbacks = callbacks

        self._run_event("stage", moment="start")
        for epoch in range(self.state.num_epochs):
            self.state.stage_epoch = epoch

            self._run_event("epoch", moment="start")
            self._run_epoch(loaders)
            self._run_event("epoch", moment="end")

            if self._check_run and self.state.epoch >= 3:
                break
            if self.state.early_stop:
                self.state.early_stop = False
                break

            self.state.epoch += 1
        self._run_event("stage", moment="end")

    def run_experiment(self, experiment: Experiment, check: bool = False):
        self._check_run = check
        self.experiment = experiment

        # jupyter source code logging hack
        # + hack to prevent cycle imports
        from catalyst.dl.experiment import BaseExperiment
        if isinstance(self.experiment, BaseExperiment) \
                and self.experiment.logdir is not None:
            expdir = Path(os.getcwd())
            logdir = Path(self.experiment.logdir)
            dump_base_experiment_code(expdir, logdir)

        try:
            for stage in self.experiment.stages:
                self._run_stage(stage)
        except (Exception, KeyboardInterrupt) as ex:
            self.state.exception = ex
            self._run_event("exception", moment=None)

        return self


__all__ = ["Runner"]
