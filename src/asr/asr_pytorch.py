#!/usr/bin/env python

# Copyright 2017 Johns Hopkins University (Shinji Watanabe)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)


import copy
import json
import logging
import math
import os
import pickle
import pdb

# chainer related
import chainer
from chainer import reporter as reporter_module
from chainer import training
from chainer.training import extensions

import torch

# spnet related
from asr_utils import adadelta_eps_decay
from asr_utils import CompareValueTrigger
from asr_utils import converter_kaldi, converter_augment
from asr_utils import delete_feat
from asr_utils import make_batchset, make_augment_batchset
from asr_utils import restore_snapshot
from e2e_asr_attctc_th import E2E
from e2e_asr_attctc_th import Loss

# for kaldi io
import kaldi_io_py
import lazy_io

# numpy related
import matplotlib
matplotlib.use('Agg')


class PytorchSeqEvaluaterKaldi(extensions.Evaluator):
    '''Custom evaluater with Kaldi reader for pytorch'''

    def __init__(self, model, iterator, target, reader, device):
        super(PytorchSeqEvaluaterKaldi, self).__init__(
            iterator, target, device=device)
        self.reader = reader
        self.model = model

    # The core part of the update routine can be customized by overriding.
    def evaluate(self):
        iterator = self._iterators['main']

        if self.eval_hook:
            self.eval_hook(self)

        if hasattr(iterator, 'reset'):
            iterator.reset()
            it = iterator
        else:
            it = copy.copy(iterator)

        summary = reporter_module.DictSummary()

        for batch in it:
            observation = {}
            with reporter_module.report_scope(observation):
                # read scp files
                # x: original json with loaded features
                #    will be converted to chainer variable later
                # batch only has one minibatch utterance, which is specified by batch[0]
                x = converter_kaldi(batch[0], self.reader)
                self.model.eval()
                self.model(x)
                delete_feat(x)

            summary.add(observation)

        return summary.compute_mean()

class PytorchSeqUpdaterKaldi(training.StandardUpdater):
    '''Custom updater with Kaldi reader for pytorch'''

    def __init__(self, model, grad_clip_threshold, train_iter, optimizer, reader, device):
        super(PytorchSeqUpdaterKaldi, self).__init__(
            train_iter, optimizer, device=None)
        self.model = model
        self.reader = reader
        self.grad_clip_threshold = grad_clip_threshold

    # The core part of the update routine can be customized by overriding.
    def update_core(self):
        # When we pass one iterator and optimizer to StandardUpdater.__init__,
        # they are automatically named 'main'.
        train_iter = self.get_iterator('main')
        optimizer = self.get_optimizer('main')

        # Get the next batch ( a list of json files)
        batch = train_iter.__next__()
        # read scp files
        # x: original json with loaded features
        #    will be converted to chainer variable later
        # batch only has one minibatch utterance, which is specified by batch[0]
        x = converter_kaldi(batch[0], self.reader)

        # Compute the loss at this time step and accumulate it
        loss = self.model(x)
        optimizer.zero_grad()  # Clear the parameter gradients
        loss.backward()  # Backprop
        loss.detach()  # Truncate the graph
        # compute the gradient norm to check if it is normal or not
        grad_norm = torch.nn.utils.clip_grad_norm(
            self.model.parameters(), self.grad_clip_threshold)
        logging.info('grad norm={}'.format(grad_norm))
        if math.isnan(grad_norm):
            logging.warning('grad norm is nan. Do not update model.')
        else:
            optimizer.step()
        delete_feat(x)


class PytorchSeqUpdaterKaldiWithAugment(PytorchSeqUpdaterKaldi):
    '''Custom updated for kaldi reader with augment data support'''
    def __init__(self, model, grad_clip_threshold, train_iter, train_augment_iter, augment_metadata, augment_ratio, optimizer, reader, device):
        super(PytorchSeqUpdaterKaldiWithAugment, self).__init__(model, grad_clip_threshold, train_iter, optimizer, reader, device=None)
        self.augment_metadata = augment_metadata
        self.train_augment_iter = train_augment_iter
        self.a2a_ratio = augment_ratio #int(self.augment_metadata['a2a_ratio'])
        self.done_augment = 0
        self.idict = self.augment_metadata['idict']
        self.odict = self.augment_metadata['odict']
        self.ifile = open(self.augment_metadata['ifilename'], 'r')
        self.ofile = open(self.augment_metadata['ofilename'], 'r')


    def update_core(self,):
        train_iter = self.get_iterator('main')
        optimizer = self.get_optimizer('main')
        
        if (self.done_augment >= self.a2a_ratio): #TODO: need a better way to switch between audio and augment
            #print('main batch')
            batch = train_iter.__next__()
            x = converter_kaldi(batch[0], self.reader)
            self.done_augment = 0
            is_aug = False
        else:
            #print('augment batch')
            batch = self.train_augment_iter.__next__()
            x = converter_augment(batch[0], self.idict, self.odict, self.ifile, self.ofile) 
            self.done_augment += 1
            is_aug = True

        # Compute the loss at this time step and accumulate it
        loss = self.model(x, is_aug = is_aug)
        optimizer.zero_grad()  # Clear the parameter gradients
        loss.backward()  # Backprop
        loss.detach()  # Truncate the graph
        # compute the gradient norm to check if it is normal or not
        grad_norm = torch.nn.utils.clip_grad_norm(
            self.model.parameters(), self.grad_clip_threshold)
        logging.info('grad norm={}'.format(grad_norm))
        if math.isnan(grad_norm):
            logging.warning('grad norm is nan. Do not update model.')
        else:
            optimizer.step()
        delete_feat(x)


def train(args):
    '''Run training'''
    # seed setting
    torch.manual_seed(args.seed)

    # debug mode setting
    # 0 would be fastest, but 1 seems to be reasonable
    # by considering reproducability
    # revmoe type check
    if args.debugmode < 2:
        chainer.config.type_check = False
        logging.info('torch type check is disabled')
    # use determinisitic computation or not
    if args.debugmode < 1:
        torch.backends.cudnn.deterministic = False
        logging.info('torch cudnn deterministic is disabled')
    else:
        torch.backends.cudnn.deterministic = True

    # check cuda availability
    if not torch.cuda.is_available():
        logging.warning('cuda is not available')

    # get input and output dimension info
    with open(args.valid_label, 'rb') as f:
        valid_json = json.load(f)['utts']
    utts = list(valid_json.keys())
    idim = int(valid_json[utts[0]]['idim'])
    odim = int(valid_json[utts[0]]['odim'])
    logging.info('#input dims : ' + str(idim))
    logging.info('#output dims: ' + str(odim))
    with open(args.train_label, 'rb') as f:
        data_json = json.load(f)
        if 'aug' in data_json:
            augment_json = data_json['aug']
            augment_idim = len(augment_json['idict'])
        else:
            augment_json = None
            augment_idim = 0


    # specify model architecture
    e2e = E2E(idim, odim, args, augment_idim = augment_idim)
    model = Loss(e2e, args.mtlalpha)

    # write model config
    if not os.path.exists(args.outdir):
        os.makedirs(args.outdir)
    model_conf = args.outdir + '/model.conf'
    with open(model_conf, 'wb') as f:
        logging.info('writing a model config file to' + model_conf)
        # TODO(watanabe) use others than pickle, possibly json, and save as a text
        pickle.dump((idim, odim, args), f)
    for key in sorted(vars(args).keys()):
        logging.info('ARGS: ' + key + ': ' + str(vars(args)[key]))

    # Set gpu
    gpu_id = int(args.gpu)
    logging.info('gpu id: ' + str(gpu_id))
    if gpu_id >= 0:
        # Make a specified GPU current
        model.cuda(gpu_id)  # Copy the model to the GPU

    # Setup an optimizer
    if args.opt == 'adadelta':
        optimizer = torch.optim.Adadelta(
            model.parameters(), rho=0.95, eps=args.eps)
    elif args.opt == 'adam':
        optimizer = torch.optim.Adam(model.parameters())

    # FIXME: TOO DIRTY HACK
    setattr(optimizer, "target", model.reporter)
    setattr(optimizer, "serialize", lambda s: model.reporter.serialize(s))

    # read json data
    with open(args.train_label, 'rb') as f:
        data_json = json.load(f)
        train_json = data_json['utts']
        if 'aug' in data_json:
            augment_json = data_json['aug']
        else:
            augment_json = None
    with open(args.valid_label, 'rb') as f:
        valid_json = json.load(f)['utts']

    # make minibatch list (variable length)
    train = make_batchset(train_json, args.batch_size,
                          args.maxlen_in, args.maxlen_out, args.minibatches)
    valid = make_batchset(valid_json, args.batch_size,
                          args.maxlen_in, args.maxlen_out, args.minibatches)
    # hack to make batch size argument as 1
    # actual batch size is included in a list
    train_iter = chainer.iterators.SerialIterator(train, 1) #TODO: why is batch size 1?
    valid_iter = chainer.iterators.SerialIterator(
        valid, 1, repeat=False, shuffle=False)

    # prepare Kaldi reader
    train_reader = lazy_io.read_dict_scp(args.train_feat)
    valid_reader = lazy_io.read_dict_scp(args.valid_feat)
    if augment_json is not None:
        train_augment, meta = make_augment_batchset(augment_json, args.batch_size,
                          args.maxlen_in, args.maxlen_out, args.minibatches)
        train_augment_iter = chainer.iterators.SerialIterator(train_augment, 1, 
                repeat=True, shuffle=True)
        assert args.augment_ratio > 0
        updater = PytorchSeqUpdaterKaldiWithAugment(model, 
                    args.grad_clip, 
                    train_iter, 
                    train_augment_iter, 
                    meta, 
                    args.augment_ratio,
                    optimizer, 
                    train_reader, 
                    gpu_id)
        trainer = training.Trainer(updater, 
                (args.epochs, 'epoch'), 
                out=args.outdir)

    else:
    # Set up a trainer
        updater = PytorchSeqUpdaterKaldi(
            model, args.grad_clip, train_iter, optimizer, train_reader, gpu_id)
        trainer = training.Trainer(
            updater, (args.epochs, 'epoch'), out=args.outdir)

    # Resume from a snapshot
    if args.resume:
        raise NotImplementedError
        chainer.serializers.load_npz(args.resume, trainer)

    # Evaluate the model with the test dataset for each epoch
    trainer.extend(PytorchSeqEvaluaterKaldi(
        model, valid_iter, model.reporter, valid_reader, device=gpu_id))

    # Take a snapshot for each specified epoch
    trainer.extend(extensions.snapshot(), trigger=(1, 'epoch'))

    # Make a plot for training and validation values
    trainer.extend(extensions.PlotReport(['main/loss', 'validation/main/loss',
                                          'main/loss_ctc', 'validation/main/loss_ctc',
                                          'main/loss_att', 'validation/main/loss_att'],
                                         'epoch', file_name='loss.png'))
    trainer.extend(extensions.PlotReport(['main/acc', 'validation/main/acc'],
                                         'epoch', file_name='acc.png'))

    # Save best models
    def torch_save(path, _):
        torch.save(model.state_dict(), path)
        torch.save(model, path + ".pkl")

    trainer.extend(extensions.snapshot_object(model, 'model.loss.best', savefun=torch_save),
                   trigger=training.triggers.MinValueTrigger('validation/main/loss'))
    trainer.extend(extensions.snapshot_object(model, 'model.acc.best', savefun=torch_save),
                   trigger=training.triggers.MaxValueTrigger('validation/main/acc'))

    # epsilon decay in the optimizer
    def torch_load(path, obj):
        model.load_state_dict(torch.load(path))
        return obj
    if args.opt == 'adadelta':
        if args.criterion == 'acc':
            trainer.extend(restore_snapshot(model, args.outdir + '/model.acc.best', load_fn=torch_load),
                           trigger=CompareValueTrigger(
                               'validation/main/acc',
                               lambda best_value, current_value: best_value > current_value))
            trainer.extend(adadelta_eps_decay(args.eps_decay),
                           trigger=CompareValueTrigger(
                               'validation/main/acc',
                               lambda best_value, current_value: best_value > current_value))
        elif args.criterion == 'loss':
            trainer.extend(restore_snapshot(model, args.outdir + '/model.loss.best', load_fn=torch_load),
                           trigger=CompareValueTrigger(
                               'validation/main/loss',
                               lambda best_value, current_value: best_value < current_value))
            trainer.extend(adadelta_eps_decay(args.eps_decay),
                           trigger=CompareValueTrigger(
                               'validation/main/loss',
                               lambda best_value, current_value: best_value < current_value))

    # Write a log of evaluation statistics for each epoch
    trainer.extend(extensions.LogReport(trigger=(100, 'iteration')))
    report_keys = ['epoch', 'iteration', 'main/loss', 'main/loss_ctc', 'main/loss_att',
                   'validation/main/loss', 'validation/main/loss_ctc', 'validation/main/loss_att',
                   'main/acc', 'validation/main/acc', 'elapsed_time']
    if args.opt == 'adadelta':
        trainer.extend(extensions.observe_value(
            'eps', lambda trainer: trainer.updater.get_optimizer('main').param_groups[0]["eps"]),
            trigger=(100, 'iteration'))
        report_keys.append('eps')
    trainer.extend(extensions.PrintReport(
        report_keys), trigger=(100, 'iteration'))

    trainer.extend(extensions.ProgressBar())

    # Run the training
    trainer.run()
    
    if isinstance(updater, PytorchSeqUpdaterKaldiWithAugment):
        updater.ifile.close()
        updater.ofile.close()


def recog(args):
    '''Run recognition'''
    # seed setting
    torch.manual_seed(args.seed)

    # read training config
    with open(args.model_conf, "rb") as f:
        logging.info('reading a model config file from' + args.model_conf)
        idim, odim, train_args = pickle.load(f)

    for key in sorted(vars(args).keys()):
        logging.info('ARGS: ' + key + ': ' + str(vars(args)[key]))

    # specify model architecture
    logging.info('reading model parameters from' + args.model)
    with open(train_args.train_label, 'rb') as f:
        data_json = json.load(f)
        if 'aug' in data_json:
            augment_json = data_json['aug']
            augment_idim = len(augment_json['idict'])
        else:
            augment_json = None
            augment_idim = 0
    e2e = E2E(idim, odim, train_args, augment_idim = augment_idim)
    model = Loss(e2e, train_args.mtlalpha)

    def cpu_loader(storage, location):
        return storage
    model.load_state_dict(torch.load(args.model, map_location=cpu_loader))

    # read rnnlm
    if args.rnnlm:
        logging.warning("rnnlm integration is not implemented in the pytorch backend")

    # prepare Kaldi reader
    reader = kaldi_io_py.read_mat_ark(args.recog_feat)

    # read json data
    with open(args.recog_label, 'rb') as f:
        recog_json = json.load(f)['utts']

    new_json = {}
    for name, feat in reader:
        y_hat = e2e.recognize(feat, args, train_args.char_list)
        y_true = map(int, recog_json[name]['tokenid'].split())

        # print out decoding result
        seq_hat = [train_args.char_list[int(idx)] for idx in y_hat]
        seq_true = [train_args.char_list[int(idx)] for idx in y_true]
        seq_hat_text = "".join(seq_hat).replace('<space>', ' ')
        seq_true_text = "".join(seq_true).replace('<space>', ' ')
        logging.info("groundtruth[%s]: " + seq_true_text, name)
        logging.info("prediction [%s]: " + seq_hat_text, name)

        # copy old json info
        new_json[name] = recog_json[name]

        # added recognition results to json
        logging.debug("dump token id")
        # TODO(karita) make consistent to chainer as idx[0] not idx
        new_json[name]['rec_tokenid'] = " ".join([str(idx) for idx in y_hat])
        logging.debug("dump token")
        new_json[name]['rec_token'] = " ".join(seq_hat)
        logging.debug("dump text")
        new_json[name]['rec_text'] = seq_hat_text

    # TODO(watanabe) fix character coding problems when saving it
    with open(args.result_label, 'wb') as f:
        f.write(json.dumps({'utts': new_json}, indent=4).encode('utf_8'))
