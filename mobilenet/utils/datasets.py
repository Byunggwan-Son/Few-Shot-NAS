import numpy as np
import torch
import torchvision.transforms as Tr
import torchvision.datasets as datasets
from copy import deepcopy
from torch.utils.data import DataLoader
from utils.distributed_sampler import TrainingSampler, InferenceSampler, trivial_batch_collator, worker_init_reset_seed


def get_datasets(args):
    train_transforms = Tr.Compose([
        Tr.RandomResizedCrop(224, scale=(0.08, 1.0)),
        Tr.RandomHorizontalFlip(),
        Tr.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4), # SPOS
        #Tr.ColorJitter(brightness=32/255, saturation=0.5, # ProxylessNAS (supernet): normal
        #Tr.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4, hue=0.1), # ProxylessNAS (re-train): strong
        Tr.ToTensor(),
        Tr.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    valid_transforms = Tr.Compose([
        Tr.Resize(256),
        Tr.CenterCrop(224),
        Tr.ToTensor(),
        Tr.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])


    trainset = datasets.ImageFolder(f"{args.data_path}/train", train_transforms)
    if args.valid_size is not None:
        validset = deepcopy(trainset)
        tr_ind, val_ind = split_trainset(trainset.targets, args.valid_size)
        trainset.samples = np.array(trainset.samples)[tr_ind].tolist()
        trainset.targets = np.array(trainset.targets)[tr_ind].tolist()
        trainset.imgs    = np.array(trainset.imgs)[tr_ind].tolist()
        
        validset.samples = np.array(validset.samples)[val_ind].tolist()
        validset.targets = np.array(validset.targets)[val_ind].tolist()
        validset.imgs    = np.array(validset.imgs)[val_ind].tolist()
        validset.transform = valid_transforms 
        validset.transforms.transform = valid_transforms
    else:
        validset = datasets.ImageFolder(f"{args.data_path}/val", valid_transforms)

    if args.num_gpus > 1:
        tr_sampler = torch.utils.data.distributed.DistributedSampler(trainset)
        te_sampler = torch.utils.data.distributed.DistributedSampler(validset)
    else:
        tr_sampler = None
    train_loader = DataLoader(
        trainset, batch_size=args.train_batch_size, num_workers=args.workers, 
        shuffle=(tr_sampler is None), pin_memory=True, sampler=tr_sampler
    )
    valid_loader = DataLoader(
        validset, batch_size=args.test_batch_size, num_workers=args.workers, 
        shuffle=False, pin_memory=True, sampler=te_sampler
    )

#    tr_sampler = torch.utils.data.sampler.BatchSampler(
#        TrainingSampler(len(trainset)), args.train_batch_size, drop_last=True,
#    )
#    te_sampler = torch.utils.data.sampler.BatchSampler(
#        InferenceSampler(len(validset)), args.test_batch_size, drop_last=False,
#    )
#
#    train_loader = DataLoader(
#        trainset, num_workers=args.workers, batch_sampler=tr_sampler,
#        collate_fn=trivial_batch_collator, worker_init_fn=worker_init_reset_seed,
#    )
#    valid_loader = DataLoader(
#        validset, num_workers=args.workers, batch_sampler=te_sampler,
#        collate_fn=trivial_batch_collator,
#    )
    return trainset, validset, train_loader, valid_loader


def split_trainset(train_labels, valid_size, n_classes=1000):
    '''
    Borrowed from ProxylessNAS
    (https://github.com/mit-han-lab/proxylessnas/blob/6e7a96b7190963e404d1cf9b37a320501e62b0a0/search/data_providers/base_provider.py#L39)
    '''
    SEED = 0 # NOTE: fixed, please don't change it

    def get_split_list(in_dim, child_num):
        in_dim_list = [in_dim // child_num] * child_num
        for _i in range(in_dim % child_num):
            in_dim_list[_i] += 1
        return in_dim_list

    train_size = len(train_labels)
    assert train_size > valid_size

    g = torch.Generator()
    g.manual_seed(SEED)  
    rand_indexes = torch.randperm(train_size, generator=g).tolist()

    train_indexes, valid_indexes = [], []
    per_class_remain = get_split_list(valid_size, n_classes)
    for idx in rand_indexes:
        label = train_labels[idx]
        if isinstance(label, float):
            label = int(label)
        elif isinstance(label, np.ndarray):
            label = np.argmax(label)
        else:
            assert isinstance(label, int)
        if per_class_remain[label] > 0:
            valid_indexes.append(idx)
            per_class_remain[label] -= 1
        else:
            train_indexes.append(idx)
    return train_indexes, valid_indexes
