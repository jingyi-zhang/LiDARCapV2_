# -*- coding: utf-8 -*-

import os
import torch
import random
import time
import wandb
import yaml
import numpy as np


project_name = os.path.basename(os.getcwd())


def torch_set_gpu(gpus):
    if type(gpus) is int:
        gpus = [gpus]

    cuda = all(gpu >= 0 for gpu in gpus)

    if cuda:
        os.environ['CUDA_VISIBLE_DEVICES'] = ','.join(
            [str(gpu) for gpu in gpus])
        # torch.backends.cudnn.benchmark = True # speed-up cudnn
        # torch.backends.cudnn.fastest = True # even more speed-up?
        hint('Launching on GPUs ' + os.environ['CUDA_VISIBLE_DEVICES'])

    else:
        hint('Launching on CPU')

    return cuda


def make_reproducible(iscuda, seed=0):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if iscuda:
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        # set True will make data load faster
        #   but, it will influence reproducible
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = True


def hint(msg):
    timestamp = f'{time.strftime("%m/%d %H:%M:%S", time.localtime(time.time()))}'
    print('\033[1m' + project_name + ' >> ' +
          timestamp + ' >> ' + '\033[0m' + msg)


# noinspection PyTypeChecker
def mesh_positions(h: int, w: int):
    gy, gx = torch.meshgrid(torch.arange(h), torch.arange(w))
    gx, gy = gx.contiguous()[None, :], gy.contiguous()[None, :]
    pos = torch.cat((gx.view(1, -1), gy.view(1, -1)))  # [2, H*W]
    return pos


def current_time(f=None):
    """
    :param f: default for log, "f" for file name
    :return: formatted time
    """
    if f == "f":
        return f'{time.strftime("%m.%d_%H.%M.%S", time.localtime(time.time()))}'
    return f'{time.strftime("%m/%d %H:%M:%S", time.localtime(time.time()))}'


def mkdir(dir):
    if not os.path.isdir(dir):
        os.makedirs(dir, exist_ok=False)


def clean_summary(filesuammry):
    """
    remove keys from wandb.log()
    Args:
        filesuammry:

    Returns:

    """
    keys = [k for k in filesuammry.keys() if not k.startswith('_')]
    for k in keys:
        filesuammry.__delitem__(k)
    return filesuammry


def watch(model):
    num = wandb.config.watch_pnum
    if type(num) is not int:
        hint(f'watch parameter num:{num} must be int')
        return
    if num <= 0:
        return
    params = list(model.modules())
    params = [x for x in params if hasattr(x, 'weight')]
    assert len(params) >= num
    idx = np.random.choice(
        len(params), size=wandb.config.watch_pnum, replace=False)
    for i in idx:
        wandb.watch(params[i], log_freq=wandb.config.log_interval, log='all')


homeserver = dict(
    WANDB_BASE_URL='',
    WANDB_ENTITY='',
    WANDB_API_KEY='',
)
v100 = dict(
    WANDB_BASE_URL='',
    WANDB_ENTITY='',
    WANDB_API_KEY='',
)
# the data generated by training, not recorded here.
cloud = dict(
    WANDB_ENTITY='',
    WANDB_API_KEY='',
)

local = dict(
    WANDB_BASE_URL='',
    WANDB_ENTITY='',
    WANDB_API_KEY='',
)


servers = dict(HOME=homeserver, V100=v100, local=local)


def login(x):
    if x in servers.keys():
        server = servers[x]
    elif x == 'cloud':
        server = cloud
    else:
        hint(f'{x} is not in profile, hence W&B it to cloud')
        server = cloud

    for k in server.keys():
        os.environ[k] = server[k]


def pdist(x, y=None):
    """
    Pairwise Distance
    Args:
        x: [bs, n, 2]
        y: [bs, n, 2]

    Returns: [bs, n, n] value in euclidean *square* distance

    """
    # B, n, two = x.shape
    x = x.float()  # [bs, n, 2]

    x_norm = (x ** 2).sum(-1, keepdim=True)  # [bs, n, 1]
    if y is not None:
        y = y.float()
        y_t = y.transpose(1, 2)  # [bs, 2, n]
        y_norm = (y ** 2).sum(-1, keepdim=True).transpose(1, 2)  # [bs, 1, n]
    else:
        y_t = x.transpose(1, 2)  # [bs, 2, n]
        y_norm = x_norm.transpose(1, 2)  # [bs, 1, n]

    dist = x_norm + y_norm - 2.0 * torch.matmul(x, y_t)  # [bs, n, n]
    return dist


def mean(lis): return sum(lis) / len(lis)


def eps(x): return x + 1e-8


def load_configs(configs):
    with open(configs, 'r') as stream:
        try:
            x = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print(exc)
        return x


def find_run(run, dir='output'):
    runs = os.listdir(dir)
    runs = [r for r in runs if run in r]
    if len(runs) <= 0:
        hint(f'Not exist run name contain : {run}')
        exit(-1)
    elif len(runs) >= 2:
        hint(f'{len(runs)} runs name contain : {run}')
        hint(f'I will return the first one : {runs[-1]}')
    else:
        hint(f'Success match {runs[-1]}')
    return runs[-1]


def reserve_mem(block_ratio=0.90):
    try:
        gpuid = int(os.environ["CUDA_VISIBLE_DEVICES"])
    except BaseException:
        gpuid = -1

    smi = (
        os.popen("nvidia-smi "
                 "--query-gpu=memory.total,memory.used "
                 "--format=csv,nounits,noheader")
        .read()
        .strip()
        .replace("\n", ",")
        .replace(" ", "")
        .split(",")
    )
    total, used = int(smi[2 * gpuid]), int(smi[2 * gpuid + 1])
    max_mem = int(total * block_ratio)
    block_mem = max_mem - used
    x = torch.rand((256, 1024, block_mem)).cuda()
    x = torch.rand((2, 2)).cuda()
    hint(f"tota:{total} "
         f"used:{used} "
         f"avai:{max_mem} "
         f"rate:{block_ratio} "
         f"memo:{block_mem}")
