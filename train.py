# # -- coding: utf-8 --**
import os
# import sys
# sys.path.append("..")

import numpy as np
import torch
from torch.cuda.amp import autocast, GradScaler
import shutil
import traceback
import importlib
import copy
import argparse
import json
import Tools.utils as utils


class Train_Session(utils.Session):
    def _build_model(self):
        super()._build_model()
        model_config = self.config['Model']
        train_config = self.config['Train']

        # load model
        param_list = []
        self.net = importlib.import_module(f"Tools.Model.{model_config['name']}").Model(**model_config)
        self.net = self.net.to(self.device)
        param_list = [
            {'params':self.net.parameters(), 'lr':train_config['lr'], 'weight_decay':train_config['weight_decay']},]

        # load the enhance subnet(NSN)
        if model_config.get('enhance', False):
            self.enhance_criterion = importlib.import_module('Tools.Model.Enhance.NSN').NSN_Loss(patch_size=model_config.get('patch_size', 5), 
                                                                                                E=model_config.get('E', 0.7), 
                                                                                                e=model_config.get('e', 4), 
                                                                                                n=model_config.get('n', 10))
            self.enhance_optimizer = torch.optim.Adam(self.net.nsn.parameters(), lr=1e-2, weight_decay=1e-4)
            param_list = [
                    {'params':self.net.nsn.parameters(), 'lr':train_config['lr'], 'weight_decay':train_config['weight_decay']},
                    {'params':self.net.fsn.parameters(), 'lr':train_config['lr'], 'weight_decay':train_config['weight_decay']}
            ]

        self.criterion = torch.nn.CrossEntropyLoss()

        self.optimizer = torch.optim.Adam(param_list)
      
        params = np.sum([p.numel() for p in self.net.parameters()]).item()
        self.logger.info(f"Loaded {model_config['name']} parameters : {params:.3e}")

    def _batch(self, item, mode='train'):
        with autocast():
            data, label = item['data'].to(self.device), item['label'].to(self.device)
            output = self.net(data)
            score = output['score'] if isinstance(output, dict) else output
            
        loss = self.criterion(score, label)
        pred = score.argmax(1)
        if mode == 'train':
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()
            # loss.backward()
            # self.optimizer.step()
            self.optimizer.zero_grad()
            # self.scheduler.step()

        return {'loss':loss.cpu(), 'pred':pred.cpu()}

    # train/eval FSN
    def _epoch(self, data_loader, epoch=None, mode='train'):
        loss = utils.Param_Detector()
        acc = utils.Param_Detector()
        time = utils.Time_Detector()
        class_pred = utils.Category_Detector(self.config['Data']['num_classes'])
        
        for i, item in enumerate(data_loader):
            result = self._batch(item, mode)
            loss.update(result['loss'])
            acc.update(result['pred'].eq(item['label']).sum(), item['label'].size(0))
            time.update(item['label'].size(0))
            class_pred.update(result['pred'], item['label'])                

        cur_lr = self.optimizer.param_groups[-1]['lr']

        if mode == 'train':
            self.logger.info(f'{mode} Epoch:{epoch}, loss:{loss.avg:.3f}, acc:{acc.avg:.1%}, lr:{cur_lr:.2e}, {time.avg:.6f}  seconds/batch')
        else:
            self.logger.info(f'{mode} Epoch:{epoch}, loss:{loss.avg:.3f}, acc:{acc.avg:.1%}, {time.avg:.6f}  seconds/batch')
        return {'loss': loss.avg.detach().numpy(),
                'acc':acc.avg.detach().numpy(),
                'class_acc':class_pred.val,
                'time':time.avg}
    
    # train enhance stage(NSN)
    def _enhance_batch(self, item):
        with autocast():
            data = item['data'].to(self.device)

            if len(data.size()) > 4:
                B, T = data.size(0), data.size(2)
                nframe = np.random.randint(0, T, size=B)
                data = data.permute(0, 2, 1, 3, 4)
                data = data[np.arange(B), nframe]
            
            enhance_result, E = self.net.nsn(data)
            enhance_loss = self.enhance_criterion(enhance_result, E, data)
            loss = enhance_loss['loss']['total']

        if self.net.nsn.training:
            self.scaler.scale(loss).backward()
            self.scaler.step(self.enhance_optimizer)
            self.scaler.update()
            self.enhance_optimizer.zero_grad()
        # elif self.plotter:
        self.plotter.images(data.detach().cpu().numpy(), win=f'Enhance_train_raw@{self.cur_scene}')
        self.plotter.images(enhance_result.detach().cpu().numpy(), win=f'Enhance_train_result@{self.cur_scene}', )
            # self.plotter.images(mask.detach().cpu().numpy(), win=f'Enhance_train_mask@{self.cur_scene}')

        return enhance_loss['loss']

    def _enhance_train(self, train_loader, test_loader):
        self.net.nsn.requires_grad_(True)
        self.net.fsn.requires_grad_(False)
        loss_dict = utils.Param_Dict()
        time = utils.Time_Detector()

        for epoch in range(self.config['Train'].get('enhance_epochs', 5)):
            for mode in ['Train', 'Eval']:
                if mode == 'Train':
                    self.net.nsn.train()
                    self.cur_scene = train_loader.dataset.scene
                    for item in train_loader:
                        loss_dict.update(self._enhance_batch(item))
                else:
                    self.net.nsn.eval()
                    self.cur_scene = test_loader.dataset.scene
                    with torch.no_grad():
                        for item in test_loader:
                            loss_dict.update(self._enhance_batch(item))
                    
                if self.plotter:
                    for k, v in loss_dict.items():
                        self.plotter.line(x=epoch, y=v.avg, win='Enahnce loss', legend=f'{mode} {k}')

                time.update(item['label'].size(0))
                loss = loss_dict['total'].avg
                cur_lr = self.enhance_optimizer.param_groups[0]['lr']
                self.logger.info(f'Enhance {mode} Epoch:{epoch}, loss:{loss:.3f}, lr:{cur_lr:.2e}, {time.avg:.2e}  seconds/batch')
        
        self.net.nsn.requires_grad_(False)
        self.net.fsn.requires_grad_(True)
        return True

    def train(self):
        # load the dataset
        train_loader = self._load_data('Train')
        eval_loader = self._load_data('Test')

        # self.scheduler = torch.optim.lr_scheduler.OneCycleLR(self.optimizer, max_lr=1e-2, 
        #                                                     steps_per_epoch = len(train_loader), 
        #                                                     epochs=self.config['Train']['num_epochs'])
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(self.optimizer, mode='min', factor=0.2, patience=2, min_lr=1e-5)

        min_loss = float('inf')
        self.scaler = GradScaler()

        # If train the S2N, we fisrt train the enhance net NSN
        if self.config['Model'].get('enhance', False):
            enhance_train_loader = self._load_data('Train', num_samples=self.config['Data']['enhance_samples'])
            enhance_test_loader = self._load_data('Test', num_samples=self.config['Data']['enhance_samples'])

            self.cur_scene = self.config['Data'].get('scene', 'all')
            self._enhance_train(enhance_train_loader, enhance_test_loader)

        # trainning
        for epoch in range(1, int(self.config['Train']['num_epochs'])+1):
            self.net.train()
            train_result = self._epoch(train_loader, epoch, mode='train')

            self.net.eval()
            self.cur_scene = self.config['Data']['scene']
            with torch.no_grad():
                eval_result = self._epoch(eval_loader, epoch, mode='eval')

            # record the result
            if self.writer:
                self.writer.add_scalar('Loss/train', train_result['loss'], epoch)
                self.writer.add_scalar('Loss/eval', eval_result['loss'], epoch)
                self.writer.add_scalar('Acc/train', train_result['acc'], epoch)
                self.writer.add_scalar('Acc/eval', eval_result['acc'], epoch)

            if epoch > 5:
                self.scheduler.step(eval_result['loss'])
            
            if epoch > (self.config['Train']['num_epochs'] - 5) and eval_result['loss'] < min_loss:
                checkpoint = copy.deepcopy(self.net.state_dict())
                max_acc = eval_result['acc']
                min_loss = eval_result['loss']
                best_epoch = epoch

        self.logger.info(f'Min loss {min_loss:.3f}, Max acc {max_acc:.1%} @ {best_epoch} epoch')
        if self.config['Recorder']['save_log']:
            checkpoint_path = os.path.join(self.log_dir, 'checkpoint.pkl')
            torch.save(checkpoint, checkpoint_path)

    def test(self):
        self.net.load_state_dict(torch.load(os.path.join(self.log_dir, 'checkpoint.pkl')))
        self.net.eval()
        with torch.no_grad():
            # test each scene
            for scene in self.config['Test']['scenes']:
                self.cur_scene = scene
                self.config['Data']['scene'] = scene
                test_loader = self._load_data('Test')
                # if scene == 'l64':
                #     import torch.nn as nn
                #     self.net.encoder['tfilter'].op['corr'].theta.data = torch.tensor(0.4)
                test_result = self._epoch(test_loader, f'@ {scene}', mode='eval')

                if self.writer:
                    test_info = f"@ {scene}, loss:{test_result['loss']:.3f}, acc:{test_result['acc']:.1%}, {test_result['time']:.6f}  seconds/batch"
                    self.writer.add_text('Test', test_info)

def set_seed(seed=1):
    import random
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = False
    torch.backends.cudnn.enable = True
    
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(1)

if __name__ == '__main__':
    args = argparse.ArgumentParser()
    args.add_argument('--config', type=str, default='DVSGesture_One_Stage_TSN')
    args.add_argument('--override', default=None, help='Arguments for overriding config')
    args = vars(args.parse_args())
    config = json.load(open(f"Tools/Config/{args['config']}.json", 'r'))
    
    set_seed(2021)

    if args['override'] is not None:
        override = args['override'].split(',')
        for item in override:
            key, value = item.split('=')
            if '.' in key:
                value = value.strip()
                if utils.isNumber(value):
                    value = float(value)
                elif value == 'True':
                    value = True
                elif value == 'False':
                    value = False
                config[key.split('.')[0].strip()][key.split('.')[1].strip()] = value
    try:
        sess = Train_Session(config)
        sess.train()
    except BaseException:
        print(traceback.format_exc())
        shutil.rmtree(sess.log_dir)
    sess.test()
    sess.close()