#! /usr/bin/env python3

import torch
from ..utils import common_functions as c_f, loss_tracker as l_t
import tqdm
import logging
import numpy as np

class BaseTrainer:
    def __init__(
        self,
        models,
        optimizers,
        batch_size,
        loss_funcs,
        mining_funcs,
        iterations_per_epoch,
        dataset,
        data_device=None,
        loss_weights=None,
        sampler=None,
        collate_fn=None,
        lr_schedulers=None,
        gradient_clippers=None,
        freeze_trunk_batchnorm=False,
        label_hierarchy_level=0,
        dataloader_num_workers=32,
        data_and_label_getter=None,
        dataset_labels=None,
        set_min_label_to_zero=False,
        end_of_iteration_hook=None,
        end_of_epoch_hook=None
    ):
        self.models = models
        self.optimizers = optimizers
        self.batch_size = batch_size
        self.loss_funcs = loss_funcs
        self.mining_funcs = mining_funcs
        self.iterations_per_epoch = iterations_per_epoch
        self.dataset = dataset
        self.data_device = data_device
        self.sampler = sampler
        self.collate_fn = collate_fn
        self.lr_schedulers = lr_schedulers
        self.gradient_clippers = gradient_clippers
        self.freeze_trunk_batchnorm = freeze_trunk_batchnorm
        self.label_hierarchy_level = label_hierarchy_level
        self.dataloader_num_workers = dataloader_num_workers
        self.loss_weights = loss_weights
        self.data_and_label_getter = data_and_label_getter
        self.dataset_labels = dataset_labels
        self.set_min_label_to_zero = set_min_label_to_zero
        self.end_of_iteration_hook = end_of_iteration_hook
        self.end_of_epoch_hook = end_of_epoch_hook
        self.loss_names = list(self.loss_funcs.keys())
        self.custom_setup()
        self.initialize_data_device()
        self.initialize_label_mapper()
        self.initialize_loss_tracker()
        self.initialize_loss_weights()
        self.initialize_data_and_label_getter()
        self.initialize_hooks()
        self.initialize_lr_schedulers()
        
    def custom_setup(self):
        pass

    def calculate_loss(self):
        raise NotImplementedError

    def update_loss_weights(self):
        pass

    def train(self, start_epoch=1, num_epochs=1):
        self.initialize_dataloader()
        for self.epoch in range(start_epoch, num_epochs+1):
            self.set_to_train()
            logging.info("TRAINING EPOCH %d" % self.epoch)
            pbar = tqdm.tqdm(range(self.iterations_per_epoch))
            for self.iteration in pbar:
                self.forward_and_backward()
                self.end_of_iteration_hook(self)
                pbar.set_description("total_loss=%.5f" % self.losses["total_loss"])
            self.step_lr_schedulers()
            if self.end_of_epoch_hook(self) is False:
                break

    def initialize_dataloader(self):
        logging.info("Initializing dataloader")
        self.dataloader = c_f.get_train_dataloader(
            self.dataset,
            self.batch_size,
            self.sampler,
            self.dataloader_num_workers,
            self.collate_fn,
        )
        logging.info("Initializing dataloader iterator")
        self.dataloader_iter = iter(self.dataloader)
        logging.info("Done creating dataloader iterator")

    def forward_and_backward(self):
        self.zero_losses()
        self.zero_grad()
        self.update_loss_weights()
        self.calculate_loss(self.get_batch())
        self.loss_tracker.update(self.loss_weights)
        self.backward()
        self.clip_gradients()
        self.step_optimizers()

    def zero_losses(self):
        for k in self.losses.keys():
            self.losses[k] = 0

    def zero_grad(self):
        for v in self.models.values():
            v.zero_grad()
        for v in self.optimizers.values():
            v.zero_grad()

    def get_batch(self):
        self.dataloader_iter, curr_batch = c_f.try_next_on_generator(self.dataloader_iter, self.dataloader)
        data, labels = self.data_and_label_getter(curr_batch)
        labels = c_f.process_label(labels, self.label_hierarchy_level, self.label_mapper)
        return self.maybe_do_pre_gradient_mining(data, labels)

    def compute_embeddings(self, data):
        trunk_output = self.get_trunk_output(data)
        embeddings = self.get_final_embeddings(trunk_output)
        return embeddings

    def get_final_embeddings(self, base_output):
        return self.models["embedder"](base_output)

    def get_trunk_output(self, data):
        return c_f.pass_data_to_model(self.models["trunk"], data, self.data_device)

    def maybe_mine_embeddings(self, embeddings, labels):
        if "post_gradient_miner" in self.mining_funcs:
            return self.mining_funcs["post_gradient_miner"](embeddings, labels)
        return None

    def maybe_do_pre_gradient_mining(self, data, labels):
        if "pre_gradient_miner" in self.mining_funcs:
            with torch.no_grad():
                self.set_to_eval()
                embeddings = self.compute_embeddings(data)
                idx = self.mining_funcs["pre_gradient_miner"](embeddings, labels)
                self.set_to_train()
                data, labels = data[idx], labels[idx]
        return data, labels

    def backward(self):
        if self.losses["total_loss"] > 0.0:
            self.losses["total_loss"].backward()

    def get_global_iteration(self):
        return self.iteration + self.iterations_per_epoch * (self.epoch - 1)

    def step_lr_schedulers(self):
        if self.lr_schedulers is not None:
            for v in self.lr_schedulers.values():
                if not isinstance(v, torch.optim.lr_scheduler.ReduceLROnPlateau):
                    v.step()

    def step_lr_plateau_schedulers(self, validation_info):
        if self.lr_schedulers is not None:
            for v in self.lr_schedulers.values():
                if isinstance(v, torch.optim.lr_scheduler.ReduceLROnPlateau):
                    v.step(validation_info)

    def step_optimizers(self):
        for v in self.optimizers.values():
            v.step()

    def clip_gradients(self):
        if self.gradient_clippers is not None:
            for v in self.gradient_clippers.values():
                v()

    def maybe_freeze_trunk_batchnorm(self):
        if self.freeze_trunk_batchnorm:
            self.models["trunk"].apply(c_f.set_layers_to_eval("BatchNorm"))

    def initialize_data_device(self):
        if self.data_device is None:
            self.data_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def initialize_label_mapper(self):
        if not self.set_min_label_to_zero:
            def label_mapper(labels, hierarchy_level):
                return labels
        else:
            label_map = c_f.get_label_map(self.dataset_labels)
            def label_mapper(labels, hierarchy_level):
                return np.array([label_map[hierarchy_level][x] for x in labels], dtype=np.int)
        self.label_mapper = label_mapper
        
    def initialize_loss_tracker(self):
        self.loss_tracker = l_t.LossTracker(self.loss_names)
        self.losses = self.loss_tracker.losses

    def initialize_data_and_label_getter(self):
        if self.data_and_label_getter is None:
            def data_and_label_getter(x):
                return x
            self.data_and_label_getter = data_and_label_getter

    def set_to_train(self):
        for k, v in self.models.items():
            v.train()
        self.maybe_freeze_trunk_batchnorm()

    def set_to_eval(self):
        for k, v in self.models.items():
            v.eval()

    def initialize_loss_weights(self):
        if self.loss_weights is None:
            self.loss_weights = {k: 1 for k in self.loss_names}

    def initialize_hooks(self):
        if self.end_of_iteration_hook is None:
            def end_of_iteration_hook(x):
                return None
            self.end_of_iteration_hook = end_of_iteration_hook
            
        if self.end_of_epoch_hook is None:
            def end_of_epoch_hook(x):
                return True
            self.end_of_epoch_hook = end_of_epoch_hook

    def initialize_lr_schedulers(self):
        if self.lr_schedulers is None:
            self.lr_schedulers = {}
