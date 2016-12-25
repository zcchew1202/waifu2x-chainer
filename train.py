import os
import math
import copy
import warnings
import numpy as np
import chainer
from chainer import cuda
from chainer import optimizers

from lib import iproc
from lib import srcnn
from lib import utils
from lib.arguments import args
from lib.pairwise_transform import pairwise_transform
from lib.loss.clipped_weighted_huber_loss import clipped_weighted_huber_loss


def resampling(datalist, offset, cfg):
    insize = cfg.crop_size + offset
    x = np.ndarray((cfg.patches * len(datalist), cfg.ch, insize, insize), dtype=np.float32)
    y = np.ndarray((cfg.patches * len(datalist), cfg.ch, cfg.crop_size, cfg.crop_size), dtype=np.float32)
    for i in range(len(datalist)):
        img = iproc.read_image_rgb_uint8(datalist[i])
        xc_batch, yc_batch = pairwise_transform(img, insize, cfg)
        x[cfg.patches * i:cfg.patches * (i + 1)] = xc_batch[:]
        y[cfg.patches * i:cfg.patches * (i + 1)] = yc_batch[:]
    return x * (1. / 255.), y * (1. / 255.)


def train_inner_epoch(model, optimizer, cfg, train_x, train_y):
    sum_loss = 0
    xp = utils.get_model_module(model)
    perm = np.random.permutation(len(train_x))
    for i in range(0, len(train_x), cfg.batch_size):
        local_perm = perm[i:i + cfg.batch_size]
        batch_x = xp.array(train_x[local_perm])
        batch_y = xp.array(train_y[local_perm])
        if cfg.test:
            for j in range(0, len(batch_x)):
                ix = iproc.to_image(batch_x[j], cfg.ch)
                iy = iproc.to_image(batch_y[j], cfg.ch)
                ix.save(os.path.join(cfg.test_dir, 'test_%d_x.png' % j))
                iy.save(os.path.join(cfg.test_dir, 'test_%d_y.png' % j))
            print '    * press any key...',
            raw_input()

        optimizer.zero_grads()
        pred = model(batch_x)
        # loss = F.mean_squared_error(pred, batch_y)
        loss = clipped_weighted_huber_loss(pred, batch_y, cfg.weight)
        loss.backward()
        optimizer.update()
        sum_loss += loss.data * len(batch_x)
    return sum_loss / len(train_x)


def validate_inner_epoch(model, cfg, valid_x, valid_y):
    sum_score = 0
    xp = utils.get_model_module(model)
    perm = np.random.permutation(len(valid_x))
    for i in range(0, len(valid_x), cfg.batch_size):
        local_perm = perm[i:i + cfg.batch_size]
        batch_x = xp.array(valid_x[local_perm])
        batch_y = xp.array(valid_y[local_perm])
        pred = model(batch_x)
        score = iproc.clipped_psnr(pred.data, batch_y)
        sum_score += score * len(batch_x)
    return sum_score / len(valid_x)


def train():
    archs = {
        'VGG_7l': srcnn.VGG_7l,
        'UpConv_7l': srcnn.UpConv_7l,
        'SRResNet_10l': srcnn.SRResNet_10l,
        'ResUpConv_10l': srcnn.ResUpConv_10l,
    }

    print '* loading datalist...',
    datalist = utils.load_datalist(args.dataset_dir)
    valid_num = int(math.ceil(args.validation_rate * len(datalist)))
    valid_list, train_list = datalist[:valid_num], datalist[valid_num:]
    print 'done'    

    print '* loading model...',
    if args.model_name is None:
        if args.method == 'noise':
            model_name = 'anime_style_noise%d_%s.npz' % (args.noise_level, args.color)
        elif args.method == 'scale':
            model_name = 'anime_style_scale_%s.npz' % args.color
        elif args.method == 'noise_scale':
            model_name = 'anime_style_noise%d_scale_%s.npz' % (args.noise_level, args.color)
    else:
        model_name = args.model_name.rstrip('.npz') + '.npz'

    model = archs[args.arch](args.ch)
    if args.finetune is not None:
        chainer.serializers.load_npz(args.finetune, model)

    if args.gpu >= 0:
        cuda.check_cuda_available()
        cuda.get_device(args.gpu).use()
        args.weight = cuda.cupy.array(args.weight)
        model.to_gpu()

    offset = utils.offset_size(model)
    optimizer = optimizers.Adam(alpha=args.learning_rate)
    optimizer.setup(model)
    print 'done'

    train_config = copy.deepcopy(args)
    valid_config = copy.deepcopy(args)
    
    print '* sampling validation dataset...',
    valid_config.max_size = 0
    valid_config.active_cropping_rate = 1.0
    valid_config.patches = train_config.validation_crops
    valid_x, valid_y = resampling(valid_list, offset, valid_config)
    print 'done'

    best_count = 0
    best_score = 0
    best_loss = np.inf
    for epoch in range(0, train_config.epoch):
        print '### epoch: %d ###' % epoch
        print '  * resampling train dataset...',
        train_x, train_y = resampling(train_list, offset, train_config)
        print 'done'
        for inner_epoch in range(0, train_config.inner_epoch):
            best_count += 1
            print '  # inner epoch: %d' % inner_epoch
            train_loss = train_inner_epoch(model, optimizer, train_config, train_x, train_y)
            valid_score = validate_inner_epoch(model, valid_config, valid_x, valid_y)
            if train_loss < best_loss:
                best_loss = train_loss
                print '    * best loss on train dataset: %f' % (train_loss)           
            if valid_score > best_score:
                best_count = 0
                best_score = valid_score
                print '    * best score on validation dataset: PSNR %f dB' % (valid_score)
                best_model = copy.deepcopy(model).to_cpu()
                epoch_name = model_name.rstrip('.npz') + '_epoch%d.npz' % epoch
                chainer.serializers.save_npz(epoch_name, best_model)
            if best_count >= train_config.lr_decay_interval:
                best_count = 0
                optimizer.alpha *= train_config.lr_decay
                if optimizer.alpha < train_config.lr_min:
                    optimizer.alpha = train_config.lr_min
                else:
                    print '    * learning rate decay: %f' % (optimizer.alpha)


warnings.filterwarnings('ignore')
if __name__ == '__main__':
    train()