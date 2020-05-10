from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import os
import argparse

import numpy as np
import shutil
import random
import time

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
import torch.nn.functional as F

import torchvision
from torchvision import datasets, models, transforms

import tensorflow as tf
import torch.nn.functional as F

from ImageDataLoader import SimpleImageLoader
from models import Res18, Res50
from efficientnet_pytorch import EfficientNet

import glob

import nsml
from nsml import DATASET_PATH, IS_ON_NSML

NUM_CLASSES = 265
if not IS_ON_NSML:
    DATASET_PATH = 'fashion_demo'

def top_n_accuracy_score(y_true, y_prob, n=5, normalize=True):
    num_obs, num_labels = y_prob.shape
    idx = num_labels - n - 1
    counter = 0
    argsorted = np.argsort(y_prob, axis=1)
    for i in range(num_obs):
        if y_true[i] in argsorted[i, idx+1:]:
            counter += 1
    if normalize:
        return counter * 1.0 / num_obs
    else:
        return counter

def top_1_accuracy_score_with_confidence(y_true, y_prob, n=5, normalize=True):
    num_obs, num_labels = y_prob.shape
    idx = num_labels - n - 1
    counter = 0
    confid = []
    y_prob_softmax = torch.softmax(y_prob, dim=1)
    argsorted = np.argsort(y_prob.cpu().numpy(), axis=1)
    for i in range(num_obs):
        if y_true[i] in argsorted[i, idx+1:]:
            counter += 1
            confid.append(y_prob_softmax[i, argsorted[i, idx+1]].item())
    if len(confid)==0:
        confid_avg = 0
        confid_min = 0
    else:
        confid_avg = sum(confid)/len(confid)
        confid_min = min(confid)
    if normalize:
        return (counter * 1.0 / num_obs)*100, confid_avg, confid_min
    else:
        return counter*100, confid_avg, confid_min


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()
    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def adjust_learning_rate(opts, optimizer, epoch):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = opts.lr * (0.1 ** (epoch // 30))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

def linear_rampup(current, rampup_length):
    if rampup_length == 0:
        return 1.0
    else:
        current = np.clip(current / rampup_length, 0.0, 1.0)
        return float(current)

def update_ema_variables(model, ema_model, alpha, global_step):
    # Use the true average until the exponential average is more correct
    alpha = min(1 - 1 / (global_step + 1), alpha)
    for ema_param, param in zip(ema_model.parameters(), model.parameters()):
        ema_param.data.mul_(alpha).add_(1 - alpha, param.data)

class SemiLoss(object):
    def __call__(self, outputs_x, targets_x, outputs_u, targets_u, epoch, final_epoch):
        probs_u = torch.softmax(outputs_u, dim=1)
        Lx = -torch.mean(torch.sum(F.log_softmax(outputs_x, dim=1) * targets_x, dim=1))
        Lu = torch.mean((probs_u - targets_u)**2)
        return Lx, Lu, opts.lambda_u

def interleave_offsets(batch, nu):
    groups = [batch // (nu + 1)] * (nu + 1)
    for x in range(batch - sum(groups)):
        groups[-x - 1] += 1
    offsets = [0]
    for g in groups:
        offsets.append(offsets[-1] + g)
    assert offsets[-1] == batch
    return offsets

def interleave(xy, batch, batch_u):
    nu = len(xy) - 1
    offsets = interleave_offsets(batch, nu)
    xy = [[v[offsets[p]:offsets[p + 1]] for p in range(nu + 1)] for v in xy]
    for i in range(1, nu + 1):
        xy[0][i], xy[i][i] = xy[i][i], xy[0][i]
    return [torch.cat(v, dim=0) for v in xy]

def split_ids(path, ratio):
    with open(path) as f:
        ids_l = []
        ids_u = []
        for i, line in enumerate(f.readlines()):
            if i == 0 or line == '' or line == '\n':
                continue
            line = line.replace('\n', '').split('\t')
            if int(line[1]) >= 0:
                ids_l.append(int(line[0]))
            else:
                ids_u.append(int(line[0]))

    ids_l = np.array(ids_l)
    ids_u = np.array(ids_u)

    perm = np.random.permutation(np.arange(len(ids_l)))
    cut = int(ratio*len(ids_l))
    train_ids = ids_l[perm][cut:]
    val_ids = ids_l[perm][:cut]

    return train_ids, val_ids, ids_u

# equalize the ratio between each class in the training set and the validation set
def split_ids_distributedly(path, ratio):
    with open(path) as f:
        ids_l = []
        for i in range(NUM_CLASSES):
           ids_l.append([])
        ids_u = []
        for i, line in enumerate(f.readlines()):
            if i == 0 or line == '' or line == '\n':
                continue
            line = line.replace('\n', '').split('\t')
            if int(line[1]) >= 0:
                ids_l[int(line[1])].append(int(line[0]))
            else:
                ids_u.append(int(line[0]))

    train_ids = np.array([])
    val_ids = np.array([])
    ids_u = np.array(ids_u)

    for i in range(NUM_CLASSES):
        ids_len = len(ids_l[i])
        ids_array = np.array(ids_l[i])
        perm = np.random.permutation(np.arange(ids_len))
        cut = int(ratio*ids_len)
        train_ids = np.concatenate((train_ids,ids_array[perm][cut:]),axis=0)
        val_ids = np.concatenate((val_ids,ids_array[perm][:cut]),axis=0)


    return train_ids, val_ids, ids_u

### NSML functions
def _infer(model, root_path, test_loader=None):
    if test_loader is None:
        test_loader = torch.utils.data.DataLoader(
            SimpleImageLoader(root_path, 'test',
                               transform=transforms.Compose([
                                   transforms.Resize(opts.imResize),
                                   transforms.CenterCrop(opts.imsize),
                                   transforms.ToTensor(),
                                   transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
                               ])), batch_size=opts.batchsize, shuffle=False, num_workers=4, pin_memory=True)
        print('loaded {} test images'.format(len(test_loader.dataset)))

    outputs = []
    s_t = time.time()
    for idx, image in enumerate(test_loader):
        if torch.cuda.is_available():
            image = image.cuda()
        _, probs = model(image)
        output = torch.argmax(probs, dim=1)
        output = output.detach().cpu().numpy()
        outputs.append(output)

    outputs = np.concatenate(outputs)
    return outputs

def bind_nsml(model):
    def save(dir_name, *args, **kwargs):
        os.makedirs(dir_name, exist_ok=True)
        state = model.state_dict()
        torch.save(state, os.path.join(dir_name, 'model.pt'))
        print('saved')

    def load(dir_name, *args, **kwargs):
        state = torch.load(os.path.join(dir_name, 'model.pt'))
        model.load_state_dict(state)
        print('loaded')

    def infer(root_path):
        return _infer(model, root_path)

    nsml.bind(save=save, load=load, infer=infer)


######################################################################
# Options
######################################################################
parser = argparse.ArgumentParser(description='Sample Product200K Training')
parser.add_argument('--start_epoch', type=int, default=1, metavar='N', help='number of start epoch (default: 1)')
parser.add_argument('--epochs', type=int, default=300, metavar='N', help='number of epochs to train (default: 200)')

# basic settings
parser.add_argument('--name',default='Fixed_threshold', type=str, help='output model name')
parser.add_argument('--gpu_ids',default='0', type=str,help='gpu_ids: e.g. 0  0,1,2  0,2')
parser.add_argument('--batchsize', default=20, type=int, help='batchsize_labeled')
parser.add_argument('--batchsize2', default=50, type=int, help='batchsize_unlabeled')
parser.add_argument('--seed', type=int, default=123, help='random seed')
parser.add_argument('--load_checkpoint', default='Fixed_threshold_best', type=str, help='checkpoint')
parser.add_argument('--load_session', default='kaist_15/fashion_eval/408', type=str, help='session name')

# basic hyper-parameters
parser.add_argument('--momentum', type=float, default=0.9, metavar='LR', help=' ')
parser.add_argument('--lr', type=float, default=5e-4, metavar='LR', help='learning rate (default: 5e-5)')
parser.add_argument('--imResize', default=256, type=int, help='')
parser.add_argument('--imsize', default=224, type=int, help='')
parser.add_argument('--lossXent', type=float, default=1, help='lossWeight for Xent')
parser.add_argument('--threshold', type=float, default=0.5, help='fixed threshold')

# arguments for logging and backup
parser.add_argument('--log_interval', type=int, default=10, metavar='N', help='logging training status')
parser.add_argument('--save_epoch', type=int, default=50, help='saving epoch interval')

# hyper-parameters for mix-match
parser.add_argument('--alpha', default=0.75, type=float)
parser.add_argument('--lambda-u', default=150, type=float)
parser.add_argument('--T', default=0.5, type=float)

# hyper-parameters for ema model
parser.add_argument('--ema-decay', default=0.999, type=float, metavar='ALPHA', help='ema variable decay rate (default: 0.999)')

### DO NOT MODIFY THIS BLOCK ###
# arguments for nsml
parser.add_argument('--pause', type=int, default=0)
parser.add_argument('--mode', type=str, default='train')
################################

def main():
    global opts
    opts = parser.parse_args()
    opts.cuda = 0

    # Set GPU
    seed = opts.seed
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    print(torch.cuda.device_count())


    os.environ['CUDA_VISIBLE_DEVICES'] = opts.gpu_ids
    use_gpu = torch.cuda.is_available()
    if use_gpu:
        opts.cuda = 1
        print("Currently using GPU {}".format(opts.gpu_ids))
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(seed)
    else:
        print("Currently using CPU (GPU is highly recommended)")

    model = EfficientNet.from_pretrained('efficientnet-b3')

    model.eval()

    parameters = filter(lambda p: p.requires_grad, model.parameters())
    n_parameters = sum([p.data.nelement() for p in model.parameters()])
    print('  + Number of params: {}'.format(n_parameters))

    if use_gpu:
        model.cuda()

    ### DO NOT MODIFY THIS BLOCK ###
    if IS_ON_NSML:
        bind_nsml(model)
        if opts.pause:
            nsml.paused(scope=locals())
    ################################

    ### load model from nsml if needed ###
    # nsml.load(checkpoint = 'opts.load_checkpoint', session = 'opts.load_session')

    if opts.mode == 'train':
        model.train()
        # Set dataloader
        train_ids, val_ids, unl_ids = split_ids(os.path.join(DATASET_PATH, 'train/train_label'), 0.2)
        print('found {} train, {} validation and {} unlabeled images'.format(len(train_ids), len(val_ids), len(unl_ids)))
        train_loader = torch.utils.data.DataLoader(
            SimpleImageLoader(DATASET_PATH, 'train', train_ids,
                              transform=transforms.Compose([
                                  transforms.Resize(opts.imResize),
                                  transforms.RandomResizedCrop(opts.imsize),
                                  transforms.RandomHorizontalFlip(),
                                  transforms.RandomVerticalFlip(),
                                  transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),])),
                                batch_size=opts.batchsize, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
        print('train_loader done')

        unlabel_loader = torch.utils.data.DataLoader(
            SimpleImageLoader(DATASET_PATH, 'unlabel', unl_ids,
                              transform=transforms.Compose([
                                  transforms.Resize(opts.imResize),
                                  transforms.RandomResizedCrop(opts.imsize),
                                  transforms.RandomHorizontalFlip(),
                                  transforms.RandomVerticalFlip(),
                                  transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),])),
                                batch_size=opts.batchsize2, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
        print('unlabel_loader done')

        validation_loader = torch.utils.data.DataLoader(
            SimpleImageLoader(DATASET_PATH, 'val', val_ids,
                               transform=transforms.Compose([
                                   transforms.Resize(opts.imResize),
                                   transforms.CenterCrop(opts.imsize),
                                   transforms.ToTensor(),
                                   transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),])),
                               batch_size=opts.batchsize2, shuffle=False, num_workers=4, pin_memory=True, drop_last=False)
        print('validation_loader done')

        # Set optimizer
        optimizer = optim.SGD(model.parameters(), lr=opts.lr, momentum = opts.momentum, weight_decay = 0.0004)

        # INSTANTIATE LOSS CLASS
        train_criterion = SemiLoss()

        # Train and Validation
        best_acc = -1
        for epoch in range(opts.start_epoch, opts.epochs + 1):
            print('start training')
            loss, _, _ = train(opts, train_loader, unlabel_loader, model, train_criterion, optimizer, epoch, use_gpu)

            print('start validation')
            acc_top1, acc_top5 = validation(opts, validation_loader, model, epoch, use_gpu)
            is_best = acc_top1 > best_acc
            best_acc = max(acc_top1, best_acc)
            nsml.report(summary=True, train_loss= loss, val_acc_top1= acc_top1, val_acc_top5=acc_top5, step=epoch)
            if is_best:
                print('saving best checkpoint...')
                if IS_ON_NSML:
                    nsml.save(opts.name + '_best')
                else:
                    torch.save(model.state_dict(), os.path.join('runs', opts.name + '_best'))
            if (epoch + 1) % opts.save_epoch == 0:
                if IS_ON_NSML:
                    nsml.save(opts.name + '_e{}'.format(epoch))
                else:
                    torch.save(model.state_dict(), os.path.join('runs', opts.name + '_e{}'.format(epoch)))


def train(opts, train_loader, unlabel_loader, model, criterion, optimizer, epoch, use_gpu):
    losses = AverageMeter()
    losses_x = AverageMeter()
    losses_un = AverageMeter()
    good_ulb = AverageMeter()
    weight_scale = AverageMeter()
    acc_top1 = AverageMeter()
    acc_top5 = AverageMeter()

    avg_loss = 0.0
    avg_top1 = 0.0
    avg_top5 = 0.0

    model.train()

    nCnt =0
    labeled_train_iter = iter(train_loader)
    unlabeled_train_iter = iter(unlabel_loader)

    for batch_idx in range(len(train_loader)):
        try:
            data = labeled_train_iter.next()
            inputs_x, targets_x = data
        except:
            labeled_train_iter = iter(train_loader)
            data = labeled_train_iter.next()
            inputs_x, targets_x = data
        try:
            data = unlabeled_train_iter.next()
            inputs_u1, inputs_u2 = data
        except:
            unlabeled_train_iter = iter(unlabel_loader)
            data = unlabeled_train_iter.next()
            inputs_u1, inputs_u2 = data

        batch_size = inputs_x.size(0)
        batch_size_u = inputs_u1.size(0)
        # Transform label to one-hot
        classno = NUM_CLASSES
        targets_org = targets_x
        targets_x = torch.zeros(batch_size, classno).scatter_(1, targets_x.view(-1,1), 1)

        if use_gpu :
            inputs_x, targets_x = inputs_x.cuda(), targets_x.cuda()
            inputs_u1, inputs_u2 = inputs_u1.cuda(), inputs_u2.cuda()
        inputs_x, targets_x = Variable(inputs_x), Variable(targets_x)
        inputs_u1, inputs_u2 = Variable(inputs_u1), Variable(inputs_u2)

        mixup_idx = []

        with torch.no_grad():
            embed_u1, pred_u1 = model(inputs_u1)
            embed_u2, pred_u2 = model(inputs_u2)
            pred_u_all = (torch.softmax(pred_u1, dim=1) + torch.softmax(pred_u2, dim=1)) / 2

            ### applying fixed threshold policy
            crit = torch.max(pred_u_all, axis=1)

            for i in range(int(crit[0].shape[0])):
                if crit[0][i] >= opts.threshold:
                    mixup_idx.append(i)

            pt = pred_u_all**(1/opts.T)
            targets_u = pt / pt.sum(dim=1, keepdim=True)
            targets_u = targets_u.detach()


        inputs_u1 = inputs_u1[mixup_idx]
        inputs_u2 = inputs_u2[mixup_idx]
        targets_u = targets_u[mixup_idx]

        good_ulb.update(len(mixup_idx)/opts.batchsize2)

        # mixup
        all_inputs = torch.cat([inputs_x, inputs_u1, inputs_u2], dim=0)
        all_targets = torch.cat([targets_x, targets_u, targets_u], dim=0)

        lamda = np.random.beta(opts.alpha, opts.alpha)
        lamda= max(lamda, 1-lamda)
        newidx = torch.randperm(all_inputs.size(0))
        input_a, input_b = all_inputs, all_inputs[newidx]
        target_a, target_b = all_targets, all_targets[newidx]

        mixed_input = lamda * input_a + (1 - lamda) * input_b
        mixed_target = lamda * target_a + (1 - lamda) * target_b

        mixed_input = list(torch.split(mixed_input, batch_size))

        optimizer.zero_grad()

        fea, logits_temp = model(mixed_input[0])

        if len(mixup_idx) != 0:
            logits = [logits_temp]
            for newinput in mixed_input[1:]:
                fea, logits_temp = model(newinput)
                logits.append(logits_temp)

            logits_x = logits[0]
            logits_u = torch.cat(logits[1:], dim=0)

            loss_x, loss_un, weigts_mixing = criterion(logits_x, mixed_target[:batch_size], logits_u, mixed_target[batch_size:], epoch+batch_idx/len(train_loader), opts.epochs)
            loss = loss_x + weigts_mixing * loss_un
            losses.update(loss.item(), inputs_x.size(0))
            losses_x.update(loss_x.item(), inputs_x.size(0))
            losses_un.update(loss_un.item(), inputs_x.size(0))
            weight_scale.update(weigts_mixing, inputs_x.size(0))

        else:
            logits = [logits_temp]
            logits_x = logits[0]
            loss_x = -torch.mean(torch.sum(F.log_softmax(logits_x, dim=1) * targets_x, dim=1))
            loss = loss_x
            losses.update(loss.item(), inputs_x.size(0))
            losses_x.update(loss_x.item(), inputs_x.size(0))
            losses_un.update(0, inputs_x.size(0))
            weight_scale.update(opts.batchsize2, inputs_x.size(0))



        # compute gradient and do SGD step
        loss.backward()
        optimizer.step()

        with torch.no_grad():
            # compute guessed labels of unlabel samples
            embed_x, pred_x1 = model(inputs_x)

        acc_top1b, confid_avg, confid_min = top_1_accuracy_score_with_confidence(targets_org.data.cpu().numpy(), pred_x1.data, n=1)
        acc_top5b = top_n_accuracy_score(targets_org.data.cpu().numpy(), pred_x1.data.cpu().numpy(), n=5)*100
        acc_top1.update(torch.as_tensor(acc_top1b), inputs_x.size(0))
        acc_top5.update(torch.as_tensor(acc_top5b), inputs_x.size(0))

        avg_loss += loss.item()
        avg_top1 += acc_top1b
        avg_top5 += acc_top5b

        if batch_idx % opts.log_interval == 0:
            print('Train Epoch:{} [{}/{}] Loss:{:.4f}({:.4f}) Top-1:{:.2f}%({:.2f}%) Top-5:{:.2f}%({:.2f}%)'.format(
                epoch, batch_idx *inputs_x.size(0), len(train_loader.dataset), losses.val, losses.avg, acc_top1.val, acc_top1.avg, acc_top5.val, acc_top5.avg))
            if batch_idx!=0:
                nsml.report(summary=True, good_unlabeled = good_ulb.avg, step=epoch+batch_idx/len(train_loader))
        if confid_avg!=0:
            nsml.report(summary = True, train_confidence_avg = confid_avg, train_confidence_min = confid_min, step = epoch+batch_idx/len(train_loader))
        nCnt += 1
        nsml.report(summary=True, losses_x = losses_x.avg, losses_un = losses_un.avg*150,  step = epoch+batch_idx/len(train_loader))

    avg_loss =  float(avg_loss/nCnt)
    avg_top1 = float(avg_top1/nCnt)
    avg_top5 = float(avg_top5/nCnt)

    nsml.report(summary=True, train_acc_top1= avg_top1, train_acc_top5=avg_top5, step=epoch)
    return  avg_loss, avg_top1, avg_top5


def validation(opts, validation_loader, model, epoch, use_gpu):
    model.eval()
    avg_top1= 0.0
    avg_top5 = 0.0
    nCnt =0
    with torch.no_grad():
        for batch_idx, data in enumerate(validation_loader):
            inputs, labels = data
            if use_gpu :
                inputs = inputs.cuda()
            inputs = Variable(inputs)
            nCnt +=1
            embed_fea, preds = model(inputs)

            acc_top1, confid_avg, confid_min = top_1_accuracy_score_with_confidence(labels.numpy(), preds.data, n=1)
            acc_top5 = top_n_accuracy_score(labels.numpy(), preds.data.cpu().numpy(), n=5)*100
            avg_top1 += acc_top1
            avg_top5 += acc_top5

        avg_top1 = float(avg_top1/nCnt)
        avg_top5= float(avg_top5/nCnt)
        print('Test Epoch:{} Top1_acc_val:{:.2f}% Top5_acc_val:{:.2f}% '.format(epoch, avg_top1, avg_top5))
    nsml.report(summary = True, valid_confidence_avg = confid_avg, valid_confidence_min = confid_min, step = epoch)

    return avg_top1, avg_top5



if __name__ == '__main__':
    main()
