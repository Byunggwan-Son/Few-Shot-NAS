import os
import sys
import time
import logging
import argparse
import random
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from copy import deepcopy

sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))
### from Detectron2 ###
import utils.comm as comm

from utils.datasets import get_datasets
from utils.optimizers import get_optimizer_scheduler
from utils.losses import get_losses
from utils.evaluator import Evaluator
from models.OneShot import SuperNet
from models.layers import SearchSpaceNames

from torch.utils.tensorboard import SummaryWriter

def arch_uniform_sampling(choices):
    return [np.random.choice(ops) for ops in choices]

def get_uniform_sample_cand(model, timeout=500):
    fl, fu = 290, 330
    for i in range(timeout):
        cand = arch_uniform_sampling(model.module.choices)
        flop = model.module.get_flops(cand)
        if fl*1e6 <= flop <= fu*1e6:
            return cand
    return arch_uniform_sampling(model.module.choices)

def do_train(args, model, logger):
    trainset, validset, train_loader, valid_loader = get_datasets(args)
    logger.info("Trainset Size: {:7d}".format(len(trainset)))
    logger.info("Validset Size: {:7d}".format(len(validset)))
    logger.info("{}".format(trainset.transform))

    iters_per_epoch = len(train_loader) 
    args.max_iter   = iters_per_epoch * args.max_epoch

    optimizer, scheduler = get_optimizer_scheduler(args, model)
    criterion = get_losses(args).cuda(args.gpu)

    logger.info(f"--> START {args.save_name}")
    model.train()
    storages = {"CE": 0}
    interval_iter_verbose = iters_per_epoch // 10

    writer = None
    if comm.is_main_process():
        writer = SummaryWriter(f'./tb_logs/supernet/{args.tag}')

    ep = 1
    train_iters = iter(train_loader)
    for it in range(1, args.max_iter+1):
        try:
            img, gt = next(train_iters)
        except:
            train_iters = iter(train_loader)
            img, gt = next(train_iters)

        rand_arch = get_uniform_sample_cand(model)
        if args.num_gpus > 1:
            rand_arch = torch.tensor(rand_arch).cuda(args.gpu)
            torch.distributed.broadcast(rand_arch, 0)
            comm.synchronize()

        logits = model(img.cuda(args.gpu, non_blocking=True), rand_arch)
        loss = criterion(logits, gt.cuda(args.gpu, non_blocking=True))

        optimizer.zero_grad()
        loss.backward()
        #nn.utils.clip_grad_value_(model.parameters(), args.grad_clip)
        optimizer.step()
        scheduler.step() 
        storages["CE"] += loss.item()

        if writer is not None:
            writer.add_scalar('loss', loss.item(), it)

        if it % interval_iter_verbose == 0:
            verbose = f"iter: {it:5d}/{args.max_iter:5d}  CE: {loss.item():.4f}  "
            logger.info(verbose)

        if it % iters_per_epoch == 0:
            for k in storages.keys(): storages[k] /= iters_per_epoch
            verbose = f"--> epoch: {ep:3d}/{args.max_epoch:3d}  avg CE: {storages['CE']:.4f}  lr: {scheduler.get_last_lr()[0]}  "
            logger.info(verbose)
            for k in storages.keys(): storages[k] = 0
            if args.num_gpus > 1:
                train_loader.sampler.set_epoch(ep)
            ep += 1

    if comm.is_main_process():
        if isinstance(model, (torch.nn.parallel.DistributedDataParallel, torch.nn.parallel.DataParallel)):
            ckpt = model.module.state_dict()
        else:
            ckpt = model.state_dict()
        info = {"state_dict": ckpt, "args": args}
        torch.save(info, args.ckpt_path)
    logger.info(f"--> END {args.save_name}")
    if writer is not None:
        writer.close()


def main_worker(gpu, ngpus_per_node, args):
    args.gpu = gpu
    if args.distributed:
        args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend='nccl', init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    logger = logging.getLogger("SuperNet Training")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if comm.is_main_process():
        formatter = logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%m/%d %H:%M:%S")
        ch = logging.StreamHandler(stream=sys.stdout)
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        fh = logging.FileHandler(args.log_path)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    for arg in vars(args):
        logger.info(f'{arg:<20}: {getattr(args, arg)}')

    search_space = SearchSpaceNames[args.search_space]
    model = SuperNet(search_space, affine=False, track_running_stats=False).cuda(args.gpu)
    if args.num_gpus > 1:
        model = DDP(model, device_ids=[args.gpu], find_unused_parameters=True)

    start_time = time.time()
    do_train(args, model, logger)
    end_time = time.time() - start_time
    hours = int(end_time // 3600)
    mins  = int((end_time % 3600) // 60)
    logger.info(f"ELAPSED TIME: {end_time:.1f}(s) = {hours:02d}(h) {mins:02d}(m)")


def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument('--tag', type=str)
    parser.add_argument('--seed', type=int, default=-1) 

    parser.add_argument('--data_path', type=str, default='../../data/imagenet')
    parser.add_argument('--save_path', type=str, default='./SuperNet')
    parser.add_argument('--search_space', type=str, default='proxyless', choices=['proxyless', 'spos', 'greedynas-v1'])
    parser.add_argument('--valid_size', type=int, default=50000, choices=[None, 50000])

    parser.add_argument("--num_gpus", type=int, default=2, help="the number of gpus")
    parser.add_argument('--workers', type=int, default=4) 
    parser.add_argument('--interval_ep_eval', type=int, default=8)

    parser.add_argument('--train_batch_size', type=int, default=1024) 
    parser.add_argument('--test_batch_size', type=int, default=256) 
    parser.add_argument('--max_epoch', type=int, default=120) 
    parser.add_argument('--learning_rate', type=float, default=0.12) 
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight_decay', type=float, default=4e-5)
    parser.add_argument('--nesterov', default=False, action='store_true')
    parser.add_argument('--lr_schedule_type', type=str, default='cosine', choices=['linear', 'poly', 'cosine'])

    parser.add_argument('--label_smooth', type=float, default=0.1)
    #parser.add_argument('--grad_clip', type=float, default=5, help='gradient clipping')
    parser.add_argument('--rank', default=0, type=int, help='node rank for distributed training')
    parser.add_argument('--gpu', default=None, type=int, help='GPU id to use.')
    return parser.parse_args()


def main():
    args = get_args()

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        os.environ["PYTHONHASHSEED"] = str(args.seed)
        cudnn.deterministic = True
        cudnn.benchmark = False #True

    args.save_name = f"{args.tag}-seed-{args.seed}"
    args.log_path  = f"{args.save_path}/logs/{args.save_name}.txt"
    args.ckpt_path = f"{args.save_path}/checkpoint/{args.save_name}.pt"
    
    args.dist_url    = "tcp://127.0.0.1:23456"
    num_machines     = 1
    ngpus_per_node   = torch.cuda.device_count()
    args.world_size  = num_machines * ngpus_per_node
    args.distributed = args.world_size > 1 

    if args.distributed:
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    else:
        main_worker(args.gpu, ngpus_per_node, args)


if __name__ == "__main__":
    main()
    
