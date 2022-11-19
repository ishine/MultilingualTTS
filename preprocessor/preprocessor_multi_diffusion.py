import os
import random
import json

import tgt
import librosa
import numpy as np
# import pyworld as pw
from scipy.interpolate import interp1d
from sklearn.preprocessing import StandardScaler
from tqdm import tqdm

import audio as Audio
from pathlib import Path
from utils.pitch_tools import get_pitch, get_cont_lf0, get_lf0_cwt
from utils.tools import dur_to_mel2ph, mel2ph_to_dur
import pdb


class BinarizationError(Exception):
    pass


class Preprocessor:
    def __init__(self, config):
        self.config = config
        self.in_dir = config["path"]["raw_path"]
        self.out_dir = config["path"]["preprocessed_path"]
        self.val_size = config["preprocessing"]["val_size"]
        self.sampling_rate = config["preprocessing"]["audio"]["sampling_rate"]
        self.hop_length = config["preprocessing"]["stft"]["hop_length"]

        self.with_f0 = config["preprocessing"]["pitch"]["with_f0"]
        self.with_f0cwt = config["preprocessing"]["pitch"]["with_f0cwt"]
        assert config["preprocessing"]["energy"]["feature"] in [
            "phoneme_level",
            "frame_level",
        ]
        self.energy_phoneme_averaging = (
            config["preprocessing"]["energy"]["feature"] == "phoneme_level"
        )
        self.energy_normalization = config["preprocessing"]["energy"]["normalization"]

        self.STFT = Audio.stft.TacotronSTFT(
            config["preprocessing"]["stft"]["filter_length"],
            config["preprocessing"]["stft"]["hop_length"],
            config["preprocessing"]["stft"]["win_length"],
            config["preprocessing"]["mel"]["n_mel_channels"],
            config["preprocessing"]["audio"]["sampling_rate"],
            config["preprocessing"]["mel"]["mel_fmin"],
            config["preprocessing"]["mel"]["mel_fmax"],
        )

    def write_metadata(self):
        data_dir = self.in_dir
        out_dir = self.out_dir
        metadata = os.path.join(out_dir, 'metadata.csv')
        if not os.path.exists(metadata):
            wav_fname_list = [str(f) for f in list(Path(data_dir).rglob('*/*/[!.]*.wav'))] #ignore hidden file
            lines = []
            for wav_fname in wav_fname_list:
                print(wav_fname)
                basename = wav_fname.split('/')[-1].replace('.wav', '')
                sid = wav_fname.split('/')[-2]
                language = wav_fname.split('/')[-3]
                txt_fname = wav_fname.replace('.wav', '.txt')
                if not os.path.exists(txt_fname):
                    txt_fname = wav_fname.replace('.wav', '.lab')
                with open(txt_fname, 'r') as f:
                    text = f.readline().strip()
                    f.close()
                lines.append('{}|{}|{}|{}'.format(basename, language, sid, text))
            with open(metadata, 'wt') as f:
                f.writelines('\n'.join(lines))
                f.close()


    def build_from_path_paralel(self):
        os.makedirs((os.path.join(self.out_dir, "mel")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "f0")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "pitch")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "cwt_spec")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "cwt_scales")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "f0cwt_mean_std")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "energy")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "duration")), exist_ok=True)
        os.makedirs((os.path.join(self.out_dir, "mel2ph")), exist_ok=True)

        print("Processing Data ...")
        out = list()
        train = list()
        val = list()
        n_frames = 0
        max_seq_len = -float('inf')
        mel_min = np.ones(80) * float('inf')
        mel_max = np.ones(80) * -float('inf')
        f0s = []
        energy_scaler = StandardScaler()

        # Compute pitch, energy, duration, and mel-spectrogram
        languages = {}
        speakers = {}
        index_spk = 0
        info_list = []
        user_list = []
        lang_list = []
        with open(os.path.join(self.out_dir, 'metadata.csv'), encoding='utf-8') as f:
            for line in f:
                tmp = line.strip().split('|') # basename, lang, user_id, raw_text
                info_list.append(tmp)
                user_list.append(tmp[2])
                lang_list.append(tmp[1])

        ########################
        results = []
        for info_l in info_list:
            print(info_l)
            tmp = self.process_utterance_paralel(info_l)
            results.append(tmp)
        ########################
        for ret in results:
            if ret is None:
                continue
            else:
                info, f0, energy, n, m_min, m_max = ret

            out.append(info)

            if len(f0) > 0:
                f0s.append(f0)
            if len(energy) > 0:
                energy_scaler.partial_fit(energy.reshape((-1, 1)))
            mel_min = np.minimum(mel_min, m_min)
            mel_max = np.maximum(mel_max, m_max)

            if n > max_seq_len:
                max_seq_len = n

            n_frames += n


        user_unique = sorted(list(set(user_list)))
        for index_spk, speaker in enumerate(user_unique):
            speakers[speaker] = index_spk
        lang_unique = sorted(list(set(lang_list)))
        for index_lang, lang in enumerate(lang_unique):
            languages[lang] = index_lang

        print("Computing statistic quantities ...")
        if len(f0s) > 0:
            f0s = np.concatenate(f0s, 0)
            f0s = f0s[f0s != 0]
            f0_mean = np.mean(f0s).item()
            f0_std = np.std(f0s).item()

        # Perform normalization if necessary
        if self.energy_normalization:
            energy_mean = energy_scaler.mean_[0]
            energy_std = energy_scaler.scale_[0]
        else:
            # A numerical trick to avoid normalization...
            energy_mean = 0
            energy_std = 1

        energy_min, energy_max = self.normalize(
            os.path.join(self.out_dir, "energy"), energy_mean, energy_std
        )

        # Save files
        with open(os.path.join(self.out_dir, "speakers.json"), "w") as f:
            f.write(json.dumps(speakers))

        with open(os.path.join(self.out_dir, "languages.json"), "w") as f:
            f.write(json.dumps(languages))

        with open(os.path.join(self.out_dir, "stats.json"), "w") as f:
            stats = {
                "f0": [
                    float(f0_mean),
                    float(f0_std),
                ],
                "energy": [
                    float(energy_min),
                    float(energy_max),
                    float(energy_mean),
                    float(energy_std),
                ],
                "spec_min": mel_min.tolist(),
                "spec_max": mel_max.tolist(),
                "max_seq_len": max_seq_len,
            }
            f.write(json.dumps(stats))

        print(
            "Total time: {} hours".format(
                n_frames * self.hop_length / self.sampling_rate / 3600
            )
        )

        random.shuffle(out)
        out = [r for r in out if r is not None]
        # train = out[self.val_size :]
        train = out
        val = out[: self.val_size]

        # Write metadata
        with open(os.path.join(self.out_dir, "train.txt"), "w", encoding="utf-8") as f:
            for m in train:
                f.write(m + "\n")
        with open(os.path.join(self.out_dir, "val.txt"), "w", encoding="utf-8") as f:
            for m in val:
                f.write(m + "\n")

        return out

    def process_utterance(self, speaker, basename):
        wav_path = os.path.join(self.in_dir, speaker, "{}.wav".format(basename))
        text_path = os.path.join(self.in_dir, speaker, "{}.lab".format(basename))
        tg_path = os.path.join(
            self.out_dir, "TextGrid", speaker, "{}.TextGrid".format(basename)
        )

        # Get alignments
        textgrid = tgt.io.read_textgrid(tg_path)
        phone, duration, mel2ph, start, end = self.get_alignment(
            textgrid.get_tier_by_name("phones")
        )
        text = "{" + " ".join(phone) + "}"
        if start >= end:
            return None

        # Read and trim wav files
        wav, _ = librosa.load(wav_path)
        wav = wav[
            int(self.sampling_rate * start) : int(self.sampling_rate * end)
        ].astype(np.float32)

        # Read raw text
        with open(text_path, "r") as f:
            raw_text = f.readline().strip("\n")

        # Compute mel-scale spectrogram and energy
        mel_spectrogram, energy = Audio.tools.get_mel_from_wav(wav, self.STFT)
        mel_spectrogram = mel_spectrogram[:, : sum(duration)]
        energy = energy[: sum(duration)]

        # Compute pitch
        if self.with_f0:
            f0, pitch = self.get_pitch(wav, mel_spectrogram.T)
            if self.with_f0cwt:
                cwt_spec, cwt_scales, f0cwt_mean_std = self.get_f0cwt(f0)

        # Save files
        dur_filename = "{}-duration-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "duration", dur_filename), duration)

        mel2ph_filename = "{}-mel2ph-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "mel2ph", mel2ph_filename), mel2ph)

        f0_filename = "{}-f0-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "f0", f0_filename), f0)

        pitch_filename = "{}-pitch-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "pitch", pitch_filename), pitch)

        cwt_spec_filename = "{}-cwt_spec-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "cwt_spec", cwt_spec_filename), cwt_spec)

        cwt_scales_filename = "{}-cwt_scales-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "cwt_scales", cwt_scales_filename), cwt_scales)

        f0cwt_mean_std_filename = "{}-f0cwt_mean_std-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "f0cwt_mean_std", f0cwt_mean_std_filename), f0cwt_mean_std)

        energy_filename = "{}-energy-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "energy", energy_filename), energy)

        mel_filename = "{}-mel-{}.npy".format(speaker, basename)
        np.save(
            os.path.join(self.out_dir, "mel", mel_filename),
            mel_spectrogram.T,
        )

        return (
            "|".join([basename, speaker, text, raw_text]),
            f0,
            self.remove_outlier(energy),
            mel_spectrogram.shape[1],
            np.min(mel_spectrogram, axis=1),
            np.max(mel_spectrogram, axis=1),
        )

    def get_tg_path(self, info):
        basename, lang, speaker, raw_text = info 
        tg_path = ""
        if lang == "Vie":
            tg_path = os.path.join(
                self.out_dir, "Multi_TextGrid", lang, "TextGrid", speaker, "{}.TextGrid".format(basename.replace("_", "-"))
            )
            # print(tg_path)
        if lang == "Chi" or lang == "Japan":
            tg_path = os.path.join(
                self.out_dir, "Multi_TextGrid", lang, "TextGrid", speaker, "{}.TextGrid".format(basename)
            )
            # print(tg_path)
        # elif lang == "Eng": # danh cho Libris
        #     tmp_sp = basename.split("_")[1]
        #     tg_path = os.path.join(
        #         self.out_dir, "Multi_TextGrid", lang, "TextGrid", speaker, tmp_sp, "{}.TextGrid".format(basename)
        #     )
        # elif lang == "Eng": # danh cho VTCK
        #     tg_path = os.path.join(
        #         self.out_dir, "Multi_TextGrid", lang, "TextGrid", speaker, "{}.TextGrid".format(basename)
        #     )
        elif lang == "Eng": # danh cho Libris va VTCK
            if len(basename.split("_")) == 4: # Libris
                tmp_sp = basename.split("_")[1]
                tg_path = os.path.join(
                    self.out_dir, "Multi_TextGrid", lang, "TextGrid", speaker, tmp_sp, "{}.TextGrid".format(basename)
                )
            elif len(basename.split("_")) == 2: # VTCK
                tg_path = os.path.join(
                    self.out_dir, "Multi_TextGrid", lang, "TextGrid", speaker, "{}.TextGrid".format(basename)
                )
            elif speaker == "LJSpeech": # LJSpeech
                tg_path = os.path.join(
                    self.out_dir, "Multi_TextGrid", lang, "TextGrid", speaker, "{}.TextGrid".format(basename)
                )
        if os.path.exists(tg_path):
            return tg_path
        else:
            print(tg_path)
            return None

    def add_prefix2phone(self, phone, lang):
        prefix = ""
        _silences = ["sp", "spn", "sil"]
        if lang == "Vie":
            prefix = "vn_"
        elif lang == "Eng":
            prefix = "eng_"
        elif lang == "Japan":
            # prefix = "jp_"
            prefix = ""
        elif lang == "Chi":
            prefix = "cn_"
        prefix_phone = []
        for p in phone:
            if p not in _silences:
                prefix_phone.append(prefix+p)
            else:
                prefix_phone.append(p)
        return prefix_phone

    def process_utterance_paralel(self, info):
        basename, lang, speaker, raw_text = info 
        wav_path = os.path.join(self.in_dir, lang, speaker, "{}.wav".format(basename))
        text_path = os.path.join(self.in_dir, lang, speaker, "{}.txt".format(basename))
        tg_path = self.get_tg_path(info)

        # Get alignments
        if tg_path:
            textgrid = tgt.io.read_textgrid(tg_path)
        else:
            return None
        phone, duration, mel2ph, start, end = self.get_alignment(
            textgrid.get_tier_by_name("phones")
        )
        phone = self.add_prefix2phone(phone, lang)
        text = "{" + " ".join(phone) + "}"
        if start >= end:
            return None

        # Read and trim wav files
        wav, _ = librosa.load(wav_path)
        wav = wav[
            int(self.sampling_rate * start) : int(self.sampling_rate * end)
        ].astype(np.float32)

        # Read raw text
        # with open(text_path, "r") as f:
        #     raw_text = f.readline().strip("\n")

        raw_text = raw_text.strip()
        # Compute mel-scale spectrogram and energy
        mel_spectrogram, energy = Audio.tools.get_mel_from_wav(wav, self.STFT)
        mel_spectrogram = mel_spectrogram[:, : sum(duration)]
        energy = energy[: sum(duration)]

        # Compute pitch
        if self.with_f0:
            f0, pitch = self.get_pitch(wav, mel_spectrogram.T)
            if self.with_f0cwt:
                cwt_spec, cwt_scales, f0cwt_mean_std = self.get_f0cwt(f0)

        # Save files
        dur_filename = "{}-duration-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "duration", dur_filename), duration)

        mel2ph_filename = "{}-mel2ph-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "mel2ph", mel2ph_filename), mel2ph)

        f0_filename = "{}-f0-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "f0", f0_filename), f0)

        pitch_filename = "{}-pitch-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "pitch", pitch_filename), pitch)

        cwt_spec_filename = "{}-cwt_spec-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "cwt_spec", cwt_spec_filename), cwt_spec)

        cwt_scales_filename = "{}-cwt_scales-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "cwt_scales", cwt_scales_filename), cwt_scales)

        f0cwt_mean_std_filename = "{}-f0cwt_mean_std-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "f0cwt_mean_std", f0cwt_mean_std_filename), f0cwt_mean_std)

        energy_filename = "{}-energy-{}.npy".format(speaker, basename)
        np.save(os.path.join(self.out_dir, "energy", energy_filename), energy)

        mel_filename = "{}-mel-{}.npy".format(speaker, basename)
        np.save(
            os.path.join(self.out_dir, "mel", mel_filename),
            mel_spectrogram.T,
        )
        return (
            "|".join([basename, lang, speaker, text, raw_text]),
            f0,
            self.remove_outlier(energy),
            mel_spectrogram.shape[1],
            np.min(mel_spectrogram, axis=1),
            np.max(mel_spectrogram, axis=1),
        )


    def get_alignment(self, tier):
        sil_phones = ["sil", "sp", "spn"]

        phones = []
        durations = []
        mel2ph = []
        start_time = 0
        end_time = 0
        end_idx = 0
        for t in tier._objects:
            s, e, p = t.start_time, t.end_time, t.text

            # Trim leading silences
            if phones == []:
                if p in sil_phones:
                    continue
                else:
                    start_time = s

            if p not in sil_phones:
                # For ordinary phones
                phones.append(p)
                end_time = e
                end_idx = len(phones)
            else:
                # For silent phones
                phones.append(p)

            durations.append(
                int(
                    np.round(e * self.sampling_rate / self.hop_length)
                    - np.round(s * self.sampling_rate / self.hop_length)
                )
            )

        # Trim tailing silences
        phones = phones[:end_idx]
        durations = durations[:end_idx]

        # Get mel2ph
        for ph_idx in range(len(phones)):
            mel2ph += [ph_idx + 1] * durations[ph_idx]
        assert sum(durations) == len(mel2ph)

        return phones, durations, mel2ph, start_time, end_time

    def get_pitch(self, wav, mel):
        f0, pitch_coarse = get_pitch(wav, mel, self.config)
        if sum(f0) == 0:
            raise BinarizationError("Empty f0")
        return f0, pitch_coarse

    def get_f0cwt(self, f0):
        uv, cont_lf0_lpf = get_cont_lf0(f0)
        logf0s_mean_org, logf0s_std_org = np.mean(cont_lf0_lpf), np.std(cont_lf0_lpf)
        logf0s_mean_std_org = np.array([logf0s_mean_org, logf0s_std_org])
        cont_lf0_lpf_norm = (cont_lf0_lpf - logf0s_mean_org) / logf0s_std_org
        Wavelet_lf0, scales = get_lf0_cwt(cont_lf0_lpf_norm)
        if np.any(np.isnan(Wavelet_lf0)):
            raise BinarizationError("NaN CWT")
        return Wavelet_lf0, scales, logf0s_mean_std_org

    def remove_outlier(self, values):
        values = np.array(values)
        p25 = np.percentile(values, 25)
        p75 = np.percentile(values, 75)
        lower = p25 - 1.5 * (p75 - p25)
        upper = p75 + 1.5 * (p75 - p25)
        normal_indices = np.logical_and(values > lower, values < upper)

        return values[normal_indices]

    def normalize(self, in_dir, mean, std):
        max_value = np.finfo(np.float64).min
        min_value = np.finfo(np.float64).max
        for filename in os.listdir(in_dir):
            filename = os.path.join(in_dir, filename)
            values = (np.load(filename) - mean) / std
            np.save(filename, values)

            max_value = max(max_value, max(values))
            min_value = min(min_value, min(values))

        return min_value, max_value
