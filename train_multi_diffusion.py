import argparse
import os

import torch
import yaml
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from utils.model import get_vocoder, get_param_num, get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion
from utils.model import get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion_Style
from utils.model import get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion_Style_KeepFS
from utils.model import get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion_Style_KeepFS1
from utils.model import get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion_Style_Language
from utils.tools import to_device, log_diffusion, log, synth_one_sample, synth_one_sample_multilingual_diffusion
from model import FastSpeech2Loss_MultiLingual_Diffusion
from dataset_multi import Dataset
from scipy.io.wavfile import write
from utils.model import vocoder_infer

# from TN_dataset.dataset_multi_balance import Dataset
# from TN_dataset.dataset_multi_balance_language import Dataset

from evaluate import evaluate, evaluate_multilingual_diffusion
import torch
torch.manual_seed(2022)
import pdb

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def main(args, configs):
    print("Prepare training ...")

    preprocess_config, model_config, train_config = configs

    # Get dataset
    dataset = Dataset(
        "train.txt", preprocess_config, train_config, sort=True, drop_last=True
    )
    batch_size = train_config["optimizer"]["batch_size"]
    group_size = 1  # Set this larger than 1 to enable sorting in Dataset
    assert batch_size * group_size < len(dataset)
    loader = DataLoader(
        dataset,
        batch_size=batch_size * group_size,
        shuffle=True,
        num_workers=15,
        collate_fn=dataset.collate_fn,
    )

    # Prepare model
    # model, optimizer = get_model(args, configs, device, train=True)
    # model, optimizer = get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion(args, configs, device, train=True)
    # model, optimizer = get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion_Style(args, configs, device, train=True)
    model, optimizer = get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion_Style_KeepFS(args, configs, device, train=True)
    # model, optimizer = get_model_fastSpeech2_StyleEncoder_MultiLanguage_Difffusion_Style_Language(args, configs, device, train=True)
    # print(model)
    pytorch_total_params = sum(p.numel() for p in model.parameters())
    pytorch_total_params_trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print("pytorch_total_params", pytorch_total_params)
    print("pytorch_total_params_trainable", pytorch_total_params_trainable)
    model = nn.DataParallel(model)
    Loss = FastSpeech2Loss_MultiLingual_Diffusion(preprocess_config, model_config).to(device)

    # Load vocoder
    vocoder = get_vocoder(configs, device)

    # Init logger
    for p in train_config["path"].values():
        os.makedirs(p, exist_ok=True)
    train_log_path = os.path.join(train_config["path"]["log_path"], "train")
    val_log_path = os.path.join(train_config["path"]["log_path"], "val")
    os.makedirs(train_log_path, exist_ok=True)
    os.makedirs(val_log_path, exist_ok=True)
    train_logger = SummaryWriter(train_log_path)
    val_logger = SummaryWriter(val_log_path)

    # Training
    step = args.restore_step + 1
    epoch = 1
    grad_acc_step = train_config["optimizer"]["grad_acc_step"]
    grad_clip_thresh = train_config["optimizer"]["grad_clip_thresh"]
    total_step = train_config["step"]["total_step"]
    log_step = train_config["step"]["log_step"]
    save_step = train_config["step"]["save_step"]
    synth_step = train_config["step"]["synth_step"]
    val_step = train_config["step"]["val_step"]

    outer_bar = tqdm(total=total_step, desc="Training", position=0)
    outer_bar.n = args.restore_step
    outer_bar.update()

    while True:
        inner_bar = tqdm(total=len(loader), desc="Epoch {}".format(epoch), position=1)
        for batchs in loader:
            for batch in batchs:
                batch = to_device(batch, device)
                if len(batch[0])==1: continue
                # Forward
                output = model(*(batch[2:]))

                # Cal Loss
                losses = Loss(batch, output)
                total_loss = losses[0]

                # Backward
                total_loss = total_loss / grad_acc_step
                total_loss.backward()
                if step % grad_acc_step == 0:
                    # Clipping gradients to avoid gradient explosion
                    nn.utils.clip_grad_norm_(model.parameters(), grad_clip_thresh)

                    # Update weights
                    optimizer.step_and_update_lr()
                    optimizer.zero_grad()

                if step % log_step == 0:
                    losses = [l.item() for l in losses]
                    message1 = "Step {}/{}, ".format(step, total_step)
                    message2 = "Total Loss: {:.4f}, Mel Loss: {:.4f}, Mel PostNet Loss: {:.4f}, Pitch Loss: {:.4f}, " \
                               "Energy Loss: {:.4f}, Duration Loss: {:.4f}, Noise Loss: {:.4f}".format(
                        *losses
                    )

                    with open(os.path.join(train_log_path, "log.txt"), "a") as f:
                        f.write(message1 + message2 + "\n")

                    outer_bar.write(message1 + message2)

                    log_diffusion(train_logger, step, losses=losses)
                    log_diffusion(train_logger, step, model=model)

                if step % synth_step == 0:
                    fig, wav_reconstruction, wav_prediction, tag = synth_one_sample_multilingual_diffusion(
                        batch,
                        output,
                        vocoder,
                        model_config,
                        preprocess_config,
                    )
                    log_diffusion(
                        train_logger,
                        fig=fig,
                        tag="Training/step_{}_{}".format(step, tag),
                    )
                    sampling_rate = preprocess_config["preprocessing"]["audio"][
                        "sampling_rate"
                    ]
                    log_diffusion(
                        train_logger,
                        audio=wav_reconstruction,
                        sampling_rate=sampling_rate,
                        tag="Training/step_{}_{}_reconstructed".format(step, tag),
                    )
                    log_diffusion(
                        train_logger,
                        audio=wav_prediction,
                        sampling_rate=sampling_rate,
                        tag="Training/step_{}_{}_synthesized".format(step, tag),
                    )

                if step % val_step == 0:
                    model.eval()
                    message = evaluate_multilingual_diffusion(model, step, configs, val_logger, vocoder)
                    with open(os.path.join(val_log_path, "log.txt"), "a") as f:
                        f.write(message + "\n")
                    outer_bar.write(message)

                    model.train()

                if step % save_step == 0:
                    torch.save(
                        {
                            "model": model.module.state_dict(),
                            "optimizer": optimizer._optimizer.state_dict(),
                        },
                        os.path.join(
                            train_config["path"]["ckpt_path"],
                            "{}.pth.tar".format(step),
                        ),
                    )

                if step == total_step:
                    quit()
                step += 1
                outer_bar.update(1)

            inner_bar.update(1)
        epoch += 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore_step", type=int, default=0)
    parser.add_argument(
        "-p",
        "--preprocess_config",
        type=str,
        required=True,
        help="path to preprocess.yaml",
    )
    parser.add_argument(
        "-m", "--model_config", type=str, required=True, help="path to model.yaml"
    )
    parser.add_argument(
        "-t", "--train_config", type=str, required=True, help="path to train.yaml"
    )

    parser.add_argument(
        "--model",
        type=str,
        choices=["naive", "aux", "shallow", "shallowstyle"],
        required=True,
        help="training model type",
    )
    args = parser.parse_args()
    if args.model in ["aux", "shallow", "shallowstyle"]:
        train_tag = "shallow"
    elif args.model == "naive":
        train_tag = "naive"
    else:
        raise NotImplementedError

    # Read Config
    preprocess_config = yaml.load(
        open(args.preprocess_config, "r"), Loader=yaml.FullLoader
    )
    model_config = yaml.load(open(args.model_config, "r"), Loader=yaml.FullLoader)
    train_config = yaml.load(open(args.train_config, "r"), Loader=yaml.FullLoader)
    configs = (preprocess_config, model_config, train_config)

    main(args, configs)
