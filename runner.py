import wandb
import argparse
from tqdm import tqdm
from datetime import datetime
import time
import os
from multiprocessing import Process

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import SGD
import numpy as np
from adabelief_pytorch import AdaBelief

from dataset import ArithmeticDataset, ArithmeticIterator
from adamw import AdamW
from open_ai_transformer import Transformer
from utils import add_tags

# torch.random.manual_seed(0)


"""
Argument Parsing
"""
parser = argparse.ArgumentParser(description="Replication of grokking behavior observed in Power et al.'s 'Grokking: Generalization Beyond Overfitting on Small Algorithmic Datasets")

# model hyperparameters
parser.add_argument("--num-layers", default=2, type=int, help="Number of layers in the transformer")
parser.add_argument("--num-heads", default=1, type=int, help="Number of attention heads per layer")
parser.add_argument("--d-model", default=128, type=int, help="Dimension of the model")

# training hyperparameters
parser.add_argument("--lr", default=1e-3, type=float, help="Learning rate")
parser.add_argument("--weight-decay", default=1e-5, type=float, help="Weight decay")
parser.add_argument("--beta1", default=0.9, type=float, help="AdamW beta1")
parser.add_argument("--beta2", default=0.98, type=float, help="AdamW beta2")
parser.add_argument("--use-sgd", default=False, action="store_true")
parser.add_argument("--use-adabelief", default=False, action="store_true")
parser.add_argument("--full-batch", default=False, action="store_true")
parser.add_argument("--momentum", type=float, default=0)
parser.add_argument("--log-normalized-loss", default=True, action="store_true") # TODO: fix this
parser.add_argument("--use-normalized-loss", default=False, action="store_true")
parser.add_argument("--use-regularized-loss", default=False, action="store_true")

# data hyperparameters 
parser.add_argument("--vocab-len", default=2000, type=int, help="Transformer vocab length")
parser.add_argument("--train-split", default=50, type=int, help="Train split")
parser.add_argument("--train-split-div-10", default=None, type=int, help="Train split div 10")
parser.add_argument("--embedding-noise", default=0, type=float, help="Add noise to the embedding (value e.g., 0.1)")
parser.add_argument("--switch-to-ten", default=False, action="store_true")
parser.add_argument("--random-data", default=False, action="store_true") 

# run hyperparameters
parser.add_argument("--optimization-budget", default=3e5, type=int, help="Number of training steps to run") # 3e10
parser.add_argument("--wandb-project", default="grokking", type=str, help="Wandb project name")
parser.add_argument("--log-margin-metrics-every", default=1, type=int, help="Log metrics every N steps (after first 100 steps)")
parser.add_argument("--no-logging", action="store_true", help="Disable logging to wandb")
parser.add_argument("--device", default=None, type=str, help="Device used for training.")
parser.add_argument("--resume-run-id", default=None, type=str, help="WandB run to resume.")
parser.add_argument("--load-path", default=None, type=str, help="Load this model.")
parser.add_argument("--log-hessian-metrics", default=False, action="store_true")
parser.add_argument("--num-jobs", default=1, type=int, help="Number of jobs to run on this gpu (default 1).")

arguments = parser.parse_args()
OPTIMIZATION_BUDGET = arguments.optimization_budget
LOG = not arguments.no_logging
DEVICE = arguments.device or ("cuda:0" if torch.cuda.is_available() else "cpu")
print("Using device:", DEVICE)

if arguments.train_split_div_10 is not None:
    arguments.train_split = arguments.train_split_div_10 * 10

def run(args: argparse.Namespace):
    """
    Model
    """
    # decoder-only transfrormer with causal attention masking; 2 layers, width 128, 4 attention heads
    model = Transformer(
        n_layers=args.num_layers,
        n_heads=args.num_heads,
        d_model=args.d_model,
        non_linearity="relu",
        vocab_len=args.vocab_len,
    ).float().to(DEVICE)
    if args.load_path is not None:
        model.load_state_dict(torch.load(args.load_path))

    """
    Dataset
    """
    train_dataset, val_dataset = ArithmeticDataset.splits(
        train_pct=args.train_split, 
        operator="/",
        operand_length=None,
    )

    if args.random_data:
        # train_dataset.data[:, -2] = train_dataset.data[np.random.permutation(list(range(len(train_dataset.data)))), -2]
        for i in range(min(train_dataset.data[:, 1]), max(train_dataset.data[:, 1]) + 1):
            mask = (train_dataset.data[:, 1] == i)
            train_dataset.data[mask, -2] = torch.tensor(np.random.permutation(train_dataset.data[mask][:, -2])).type_as(train_dataset.data)
        

    train_dataloader = ArithmeticIterator(
        train_dataset,
        DEVICE,
        batchsize_hint= (0 if not args.full_batch else -1), # 0 -> default (512), -1 -> full batch
    )
    val_dataloader = ArithmeticIterator(
        val_dataset,
        DEVICE,
        batchsize_hint=-1,
    )

    sample = train_dataset.tokenizer.encode("0 / 1 = 0 <|eos|>")
    zero_enc, one_enc = sample[0].item(), sample[2].item()

    full_train_X, full_train_y = None, None
    for data in train_dataloader:
        X, y = data["text"], data["target"]
        if full_train_X is None:
            full_train_X = X
            full_train_y = y
        else:
            full_train_X = torch.cat((full_train_X, X), dim=0)
            full_train_y = torch.cat((full_train_y, y), dim=0)


    """
    WandB Logging
    """
    tags = [f"d_model={args.d_model}", f"num_layers={args.num_layers}", f"num_heads={args.num_heads}"]
    date_time = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    name = f"split_{args.train_split}-decay_{args.weight_decay}-dim_{args.d_model}-{date_time}"
    aug_name, aug_tags = add_tags(
        ("full_batch-", args.full_batch),
        (f"noise_{args.embedding_noise}-", args.embedding_noise > 0),
        (f"sgd_lr_{args.lr}_mom_{args.momentum}-", args.use_sgd), 
        (f"adabelief_lr_{args.lr}_mom_{args.momentum}-", args.use_adabelief),
        ("random_data-", args.random_data),
        ("regularized_loss-", args.use_regularized_loss)
    )
    name = aug_name + name; tags += aug_tags

    if LOG:
        if args.resume_run_id is None:
            wandb.init(project=args.wandb_project, id=date_time, settings=wandb.Settings(start_method="thread"), tags=tags, name=name, config=args)
        else:
            wandb.init(id=args.resume_run_id, resume="must", project=args.wandb_project, settings=wandb.Settings(start_method="thread"), tags=tags, name=name, config=args)

    # log number of parameters 
    num_params = sum([p.numel() for p in model.parameters() if p.requires_grad])
    if LOG:
        wandb.log({"Number of Parameters": num_params})
        wandb.watch(model)
    print(f"Model has {num_params} trainable parameters.")

    # mkae weights directory if needed
    try:
        os.makedirs("weights")
    except:
        pass

    """
    Optimizer
    """
    assert not (args.use_sgd and args.use_adabelief)
    assert (args.use_regularized_loss and args.use_sgd) or not args.use_regularized_loss
    if args.use_sgd:
        optimizer = SGD(
            model.parameters(),
            lr = args.lr,
            weight_decay=args.weight_decay if not args.use_regularized_loss else 0,
            momentum=args.momentum,
        )
    elif args.use_adabelief:
        optimizer = AdaBelief(model.parameters(), lr=1e-3, eps=1e-16, betas=(0.9,0.999), weight_decouple = True, rectify = False) # try 1e-8
    else:
        optimizer = AdamW(
            model.parameters(), 
            lr=args.lr, 
            weight_decay=args.weight_decay, 
            betas=(args.beta1, args.beta2)
        )    

    """
    Criterion
    """
    class SpecialCEL(nn.Module):
        @staticmethod
        # from neel nanda's code
        def cross_entropy_high_precision(logits, labels):
            # Shapes: batch x vocab, batch
            # Cast logits to float64 because log_softmax has a float32 underflow on overly 
            # confident data and can only return multiples of 1.2e-7 (the smallest float x
            # such that 1+x is different from 1 in float32). This leads to loss spikes 
            # and dodgy gradients
            logprobs = F.log_softmax(logits.to(torch.float64), dim=-1)
            prediction_logprobs = torch.gather(logprobs, index=labels[:, None], dim=-1)
            loss = -torch.mean(prediction_logprobs)
            return loss
        def forward(self, input, target, normalize=False):
            y_hat = input[:, -2, :]
            if normalize:
                y_hat = F.normalize(y_hat, p=2, dim=1)
            return self.cross_entropy_high_precision(y_hat, target[:, -2]) 

    class SpecialCELReg(SpecialCEL):
        def __init__(self, l2_reg, model):
            super().__init__()
            self.l2_reg = l2_reg
            self.model: nn.Module = model
            self.reg_term = 0

        def forward(self, input, target, **kwargs):
            loss = super().forward(input, target, **kwargs)
            reg_term = 0
            for n, p in model.named_parameters():
                if n.split('.')[-1] == "weight":
                    reg_term += torch.sum(torch.pow(p, 2))
            reg_term /= 2
            self.reg_term = reg_term 
            return loss + self.l2_reg * reg_term
    
    # looks like \lambda for l2 reg should be the _same_ as \lambda for weight_decay. This implies  that our weight decay is actually already being divided by the learning rate 
    # in pytorch. I just checked the pytorch code, and this _is_ the case!
    reg_criterion = SpecialCELReg(args.weight_decay, model)
    # criterion = reg_criterion if args.use_regularized_loss else SpecialCEL()
    cel = nn.CrossEntropyLoss()
    criterion = lambda y_hat, target, *args, **kwargs: cel(y_hat[:, -2, :], target[:, -2])
    """
    Train
    """
    steps_per_epoch = len(train_dataloader)
    interpolated_99, interpolated_100, generalized_99, generalized_100, generalized_90 = False, False, False, False, False
    for epoch in tqdm(range(int(OPTIMIZATION_BUDGET / steps_per_epoch))):
        if epoch == 10000 and args.switch_to_ten:
            del train_dataset, train_dataloader
            del val_dataset, val_dataloader
            train_dataset, val_dataset = ArithmeticDataset.splits(
                train_pct=2, 
                operator="/",
                operand_length=None,
            )
            train_dataloader = ArithmeticIterator(
                train_dataset,
                DEVICE,
                batchsize_hint= 512 if not args.full_batch else -1, # 0 -> default (512), -1 -> full batch
            )
            val_dataloader = ArithmeticIterator(
                val_dataset,
                DEVICE,
                batchsize_hint=-1,
            )

        # eval
        val_acc = validate(args, model, train_dataloader, val_dataloader, zero_enc, one_enc, criterion, epoch)
        if val_acc > .9 and not generalized_90:
            print(f"> .9 gen achieved at train_step {epoch * len(train_dataloader)}")
            generalized_90 = True
            if LOG:
                wandb.log({
                    "Time to .9 Test Accuracy (epoch)": epoch, 
                    "Time to .9 Test Accuracy (train step)":  epoch * len(train_dataloader), 
                    "train_step":  epoch * len(train_dataloader), 
                    "epoch": epoch,
                })
        if val_acc > .99 and not generalized_99:
            print(f"> .99 gen achieved at train_step {epoch * len(train_dataloader)}")
            generalized_99 = True
            if LOG:
                wandb.log({
                    "Time to .99 Test Accuracy (epoch)": epoch, 
                    "Time to .99 Test Accuracy (train step)":  epoch * len(train_dataloader), 
                    "train_step":  epoch * len(train_dataloader), 
                    "epoch": epoch,
                })
        if val_acc == 1 and not generalized_100:
            print(f"perfect gen achieved at train_step {epoch * len(train_dataloader)}")
            generalized_100 = True
            if LOG:
                wandb.log({
                    "Time to 1 Test Accuracy (epoch)": epoch, 
                    "Time to 1 Test Accuracy (train step)":  epoch * len(train_dataloader), 
                    "train_step":  epoch * len(train_dataloader), 
                    "epoch": epoch,
                })

        # train
        model.train()
        total_train_acc = 0
        for i, batch in enumerate(train_dataloader):
            train_acc = train_step(args, model, i, train_dataloader, optimizer, reg_criterion, criterion, epoch, batch)
            total_train_acc += train_acc
            if epoch < 1000:
                validate(args, model, train_dataloader, val_dataloader, zero_enc, one_enc, criterion, epoch)
        if total_train_acc/len(train_dataloader) > .99 and not interpolated_99:
            print(f"> .99 interpolation achieved at train_step {(epoch + 1) * len(train_dataloader)}")
            interpolated_99 = True
            if LOG:
                wandb.log({
                    "Time to .99 Train Accuracy (epoch)": epoch + 1, 
                    "Time to .99 Train Accuracy (train step)":  (epoch + 1) * len(train_dataloader), 
                    "train_step":  epoch * len(train_dataloader), 
                    "epoch": epoch,
                })
        if total_train_acc/len(train_dataloader) == 1 and not interpolated_100:
            print(f"> .99 interpolation achieved at train_step {(epoch + 1) * len(train_dataloader)}")
            interpolated_100 = True
            if LOG:
                wandb.log({
                    "Time to 1 Train Accuracy (epoch)": epoch + 1, 
                    "Time to 1 Train Accuracy (train step)":  (epoch + 1) * len(train_dataloader), 
                    "train_step":  epoch * len(train_dataloader), 
                    "epoch": epoch,
                })

def train_step(args, model, i, train_dataloader, optimizer, reg_criterion, criterion, epoch, batch):
    X, y = batch['text'], batch['target'] # batch['lower_dim_embedding'], batch['target']
    X, y = X.to(DEVICE), y.to(DEVICE)
    y_hat = model(X, embedding_noise=args.embedding_noise)
    loss = criterion(y_hat, y, normalize=args.use_normalized_loss)
    train_acc = (y_hat[:, -2, :].argmax(dim=1) == y[:, -2]).float().mean().item()
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()
    with torch.no_grad():
        loss_with_reg = reg_criterion(y_hat, y, normalize=args.use_normalized_loss)
        log_dict = {
                    "Loss/train": loss.item(),
                    "Loss/train_reg_term_only":  reg_criterion.reg_term.item(), 
                    "Loss/train_with_reg": loss_with_reg.item(),
                    "Accuracy/train": train_acc,
                    "epoch": epoch,
                    "train_step": epoch * len(train_dataloader) + i
                }
        if args.log_normalized_loss:
            log_dict["Loss/train_normalized"] = criterion(y_hat, y, normalize=True).item()
        if LOG:
            total_norm, decoder_norms, linear_norm, embedding_norm = 0, 0, 0, 0
            for n, p in model.named_parameters():
                if p.requires_grad:
                    norm = p.grad.norm().item()
                    total_norm += norm
                    log_dict[f"Gradient_Norms/{n}"] = norm
            for n, p in model.named_parameters():
                if p.requires_grad:
                    log_dict[f"Gradients_Norms_Perc_Total/{n}"] = log_dict[f"Gradient_Norms/{n}"]/total_norm
                    if n.split('.')[0] == "decoder":
                        decoder_norms += log_dict[f"Gradient_Norms/{n}"]
                    elif n.split('.')[0] == "linear":
                        linear_norm += log_dict[f"Gradient_Norms/{n}"]
                    elif n.split('.')[0] == "embedding":
                        embedding_norm += log_dict[f"Gradient_Norms/{n}"]
                    else:
                        raise Exception("Unexpected Parameter Name")
            log_dict[f"Gradient_Norms_Perc_Total/decoder"] = decoder_norms / total_norm 
            log_dict[f"Gradient_Norms_Perc_Total/linear"] = linear_norm / total_norm 
            log_dict[f"Gradient_Norms_Perc_Total/embedding"] = embedding_norm / total_norm 
            wandb.log(log_dict)
        else:
            print(f"Epoch {epoch}: train loss {loss.item()}, train accuracy {train_acc}")
        return train_acc

def validate(args, model, train_dataloader, val_dataloader, zero_enc, one_enc, criterion, epoch):
    model.eval()
    with torch.no_grad():
        loss, accuracy = 0, 0
        div_1_loss, div_1_acc = 0, 0
        zero_start_loss, zero_start_acc = 0, 0
        not_zero_loss, not_zero_acc = 0, 0
        either_loss, either_acc = 0, 0
        a_eq_b_loss, a_eq_b_acc = 0, 0
        for batch in val_dataloader: # only one batch
            X, y = batch['text'], batch['target'] # batch['text'], batch['target']
            X, y = X.to(DEVICE), y.to(DEVICE)

            y_hat = model(X)
            loss += criterion(y_hat, y, normalize=args.use_normalized_loss).item()
            accuracy += (y_hat[:, -2, :].argmax(dim=1) == y[:, -2]).float().mean().item()

            zero_start_y = y[y[:, 0] == zero_enc]
            zero_start_X = X[y[:, 0] == zero_enc]
            zero_start_y_hat = model(zero_start_X)

            div_1_y = y[y[:, 2] == one_enc]
            div_1_X = X[y[:, 2] == one_enc]
            div_1_y_hat = model(div_1_X)

            not_zero_y = y[~(y[:, 0] == zero_enc)]
            not_zero_X = X[~(y[:, 0] == zero_enc)]
            not_zero_y_hat = model(not_zero_X)

            a_eq_b_y = y[y[:, 0] == y[:, 2]]
            a_eq_b_X = X[y[:, 0] == y[:, 2]]
            a_eq_b_y_hat = model(a_eq_b_X)

            either_mask = ((y[:, 2] == one_enc)*1 + (y[:, 0] == zero_enc)*1) > 0
            either_y = y[either_mask]
            either_X = X[either_mask]
            either_y_hat = model(either_X)

            div_1_loss += criterion(div_1_y_hat, div_1_y).item()
            div_1_acc += (div_1_y_hat[:, -2, :].argmax(dim=1) == div_1_y[:, -2]).float().mean().item()

            zero_start_loss += criterion(zero_start_y_hat, zero_start_y).item()
            zero_start_acc += (zero_start_y_hat[:, -2, :].argmax(dim=1) == zero_start_y[:, -2]).float().mean().item()

            not_zero_loss += criterion(not_zero_y_hat, not_zero_y).item()
            not_zero_acc += (not_zero_y_hat[:, -2, :].argmax(dim=1) == not_zero_y[:, -2]).float().mean().item()

            either_loss += criterion(either_y_hat, either_y).item()
            either_acc += (either_y_hat[:, -2, :].argmax(dim=1) == either_y[:, -2]).float().mean().item()

            a_eq_b_loss += criterion(a_eq_b_y_hat, a_eq_b_y).item()
            a_eq_b_acc += (a_eq_b_y_hat[:, -2, :].argmax(dim=1) == a_eq_b_y[:, -2]).float().mean().item()


        if LOG:
            log_dict = {
                    "Loss/val": loss / len(val_dataloader),
                    "Loss/val_div_1": div_1_loss / len(val_dataloader),
                    "Loss/val_0_div": zero_start_loss / len(val_dataloader),
                    "Loss/val_not_0": not_zero_loss / len(val_dataloader),
                    "Loss/val_either_0_1": either_loss / len(val_dataloader),
                    "Loss/val_a_eq_b": a_eq_b_loss / len(val_dataloader),
                    "Accuracy/val": accuracy / len(val_dataloader),
                    "Accuracy/val_div_1": div_1_acc / len(val_dataloader),
                    "Accuracy/val_0_div": zero_start_acc / len(val_dataloader),
                    "Accuracy/val_a_eq_b": a_eq_b_acc / len(val_dataloader),
                    "Accuracy/val_not_zero": not_zero_acc / len(val_dataloader),
                    "Accuracy/val_either_0_1": either_acc / len(val_dataloader),
                    "Percent Zero Prediction": (y_hat[:, -2, :].argmax(dim=1) == zero_enc).sum()/len(y_hat[:, -2, :]),
                    "epoch": epoch,
                    "train_step": epoch * len(train_dataloader)
                }
            if args.log_normalized_loss:
                log_dict["Loss/val_normalized"] = criterion(y_hat, y, normalize=True).item()
            wandb.log(log_dict)
        else:
            print(f"Epoch {epoch}: test loss {loss / len(val_dataloader)}, test accuracy {accuracy / len(val_dataloader)}")
        
        return accuracy / len(val_dataloader)
        # save model
        # torch.save(model.state_dict(), f"weights/{name}-LATEST-model.pt")

if __name__ == '__main__':
    # torch.multiprocessing.set_start_method('spawn')
    # wandb.setup(settings=wandb.Settings(start_method="thread"))

    # processes = []
    # for i in range(arguments.num_jobs):
    #     argument_copy = argparse.Namespace(**vars(arguments))
    #     processes.append(Process(target=run, args=(argument_copy,)))

    # for p in processes:
    #     print("starting process...")
    #     p.start()
    #     time.sleep(1)

    # for p in processes:
    #     p.join()
    run(arguments)
