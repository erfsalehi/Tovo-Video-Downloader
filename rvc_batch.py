"""Corrected batch RVC inference for Mangio-RVC v23.7.0.

The Mangio install ships an ``infer_batch_rvc.py`` that is stale for this version:
``my_utils.load_audio()`` and ``VC.pipeline()`` both gained new required
arguments, so the bundled script crashes. This is a minimal, corrected batch
runner that the Video Downloader app invokes instead.

Run it with the Mangio runtime's python and with the working directory set to the
Mangio folder, so its modules (``vc_infer_pipeline``, ``lib``, ``my_utils``) and
its model files (``hubert_base.pt``, ``rmvpe.pt``) resolve relative to cwd.

Positional args:
  f0up_key input_dir index_path f0method opt_dir model_path index_rate
  device is_half filter_radius resample_sr rms_mix_rate protect [crepe_hop_length]

Every ``.wav`` in ``input_dir`` is converted and written, under the same name,
into ``opt_dir``. Progress is reported on stdout as machine-readable lines so the
caller can update a UI per file:

  RVC_TOTAL\t<n>
  RVC_START\t<filename>
  RVC_DONE\t<filename>
  RVC_FAIL\t<filename>

Notes on robustness:
- We seed the in-memory ``formant_data`` table (as Mangio's web UI does) with
  DoFormant=0, otherwise ``my_utils.load_audio`` reads a stale/missing value and
  runs a per-file ``stftpitchshift`` via ``os.system()`` — which pops a console
  window for every clip on Windows.
- The executable part is guarded by ``if __name__ == '__main__'`` with
  ``freeze_support()`` so nothing re-spawns under Windows' spawn start method.
"""
import os
import sqlite3
import sys

import torch
from multiprocessing import cpu_count

now_dir = os.getcwd()
sys.path.append(now_dir)

from vc_infer_pipeline import VC
from lib.infer_pack.models import (
    SynthesizerTrnMs256NSFsid,
    SynthesizerTrnMs256NSFsid_nono,
    SynthesizerTrnMs768NSFsid,
    SynthesizerTrnMs768NSFsid_nono,
)
from my_utils import load_audio
from fairseq import checkpoint_utils
from scipy.io import wavfile

hubert_model = None
_formant_conn = None  # kept open for the process lifetime (shared in-memory cache)


class Config:
    """Device/precision config, copied from the bundled batch script (this part
    still works on current Mangio) and trimmed of its 16-series file-rewriting."""

    def __init__(self, device, is_half):
        self.device = device
        self.is_half = is_half
        self.n_cpu = 0
        self.gpu_name = None
        self.gpu_mem = None
        self.x_pad, self.x_query, self.x_center, self.x_max = self.device_config()

    def device_config(self):
        # Branch on the *requested* device, not global CUDA availability: the
        # user can force "cpu" on a CUDA-capable machine, in which case
        # int("cpu".split(":")[-1]) used to blow up here. Only take the GPU path
        # for an actual "cuda[:N]" device that torch can see.
        if self.device.startswith("cuda") and torch.cuda.is_available():
            i_device = int(self.device.split(":")[-1]) if ":" in self.device else 0
            self.gpu_name = torch.cuda.get_device_name(i_device)
            if (
                ("16" in self.gpu_name and "V100" not in self.gpu_name.upper())
                or "P40" in self.gpu_name.upper()
                or "1060" in self.gpu_name
                or "1070" in self.gpu_name
                or "1080" in self.gpu_name
            ):
                print("16-series / P40: forcing single precision")
                self.is_half = False
            self.gpu_mem = int(
                torch.cuda.get_device_properties(i_device).total_memory
                / 1024 / 1024 / 1024 + 0.4
            )
        elif self.device == "mps" and torch.backends.mps.is_available():
            print("Using MPS")
        else:
            # Explicit CPU request, or a cuda device torch can't actually see.
            print("Using CPU")
            self.device = "cpu"
            self.is_half = False

        if self.n_cpu == 0:
            self.n_cpu = cpu_count()

        if self.is_half:
            x_pad, x_query, x_center, x_max = 3, 10, 60, 65
        else:
            x_pad, x_query, x_center, x_max = 1, 6, 38, 41
        if self.gpu_mem is not None and self.gpu_mem <= 4:
            x_pad, x_query, x_center, x_max = 1, 5, 30, 32
        return x_pad, x_query, x_center, x_max


def setup_formant_db():
    """Ensure the in-memory formant DB exists with DoFormant=0 (mirrors
    infer-web.py). Without this, ``my_utils.load_audio`` either crashes on a
    missing table or honours a leftover DoFormant=1 and shells out to
    ``stftpitchshift`` per file, opening a console window each time."""
    global _formant_conn
    try:
        os.makedirs("TEMP", exist_ok=True)
        _formant_conn = sqlite3.connect(
            "TEMP/db:cachedb?mode=memory&cache=shared", check_same_thread=False
        )
        cur = _formant_conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS formant_data "
            "(Quefrency FLOAT, Timbre FLOAT, DoFormant INTEGER)"
        )
        cur.execute("CREATE TABLE IF NOT EXISTS stop_train (stop BOOL)")
        cur.execute("DELETE FROM formant_data")
        cur.execute(
            "INSERT INTO formant_data (Quefrency, Timbre, DoFormant) VALUES (?, ?, ?)",
            (8.0, 1.2, 0),
        )
        _formant_conn.commit()
    except Exception as e:  # never let DB setup abort a conversion
        print("  formant DB setup warning: %r" % e, flush=True)


def load_hubert():
    global hubert_model
    models, _saved_cfg, _task = checkpoint_utils.load_model_ensemble_and_task(
        ["hubert_base.pt"], suffix="",
    )
    hubert_model = models[0].to(device)
    hubert_model = hubert_model.half() if is_half else hubert_model.float()
    hubert_model.eval()


def vc_single(sid, input_audio, f0_up_key, f0_file, f0_method, file_index, index_rate):
    global tgt_sr, net_g, vc, hubert_model, version
    if input_audio is None:
        return None
    f0_up_key = int(f0_up_key)
    # load_audio now requires (file, sr, DoFormant, Quefrency, Timbre); the formant
    # values are re-read from the TEMP DB (seeded to DoFormant=0 by setup_formant_db).
    audio = load_audio(input_audio, 16000, False, 0.0, 0.0)
    times = [0, 0, 0]
    if hubert_model is None:
        load_hubert()
    if_f0 = cpt.get("f0", 1)
    audio_opt = vc.pipeline(
        hubert_model, net_g, sid, audio, input_audio, times, f0_up_key,
        f0_method, file_index, index_rate, if_f0, filter_radius, tgt_sr,
        resample_sr, rms_mix_rate, version, protect, crepe_hop_length,
        f0_file=f0_file,
    )
    return audio_opt


def get_vc(model_path):
    global n_spk, tgt_sr, net_g, vc, cpt, device, is_half, version
    print("loading pth %s" % model_path)
    cpt = torch.load(model_path, map_location="cpu")
    tgt_sr = cpt["config"][-1]
    cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # n_spk
    if_f0 = cpt.get("f0", 1)
    version = cpt.get("version", "v1")
    if version == "v1":
        net_g = (SynthesizerTrnMs256NSFsid(*cpt["config"], is_half=is_half)
                 if if_f0 == 1 else SynthesizerTrnMs256NSFsid_nono(*cpt["config"]))
    else:
        net_g = (SynthesizerTrnMs768NSFsid(*cpt["config"], is_half=is_half)
                 if if_f0 == 1 else SynthesizerTrnMs768NSFsid_nono(*cpt["config"]))
    del net_g.enc_q
    print(net_g.load_state_dict(cpt["weight"], strict=False))
    net_g.eval().to(device)
    net_g = net_g.half() if is_half else net_g.float()
    vc = VC(tgt_sr, config)
    n_spk = cpt["config"][-3]


def main():
    global f0up_key, input_path, index_path, f0method, opt_path, model_path
    global index_rate, device, is_half, filter_radius, resample_sr, rms_mix_rate
    global protect, crepe_hop_length, config

    f0up_key = sys.argv[1]
    input_path = sys.argv[2]
    index_path = sys.argv[3]
    f0method = sys.argv[4]
    opt_path = sys.argv[5]
    model_path = sys.argv[6]
    index_rate = float(sys.argv[7])
    device = sys.argv[8]
    is_half = sys.argv[9].lower() != "false"
    filter_radius = int(sys.argv[10])
    resample_sr = int(sys.argv[11])
    rms_mix_rate = float(sys.argv[12])
    protect = float(sys.argv[13])
    crepe_hop_length = int(sys.argv[14]) if len(sys.argv) > 14 else 128

    config = Config(device, is_half)
    device = config.device  # honour CPU/MPS fallback from device_config

    setup_formant_db()
    get_vc(model_path)
    os.makedirs(opt_path, exist_ok=True)

    files = [f for f in os.listdir(input_path) if f.lower().endswith(".wav")]
    print("RVC_TOTAL\t%d" % len(files), flush=True)
    for file in files:
        print("RVC_START\t%s" % file, flush=True)
        file_path = os.path.join(input_path, file)
        try:
            wav_opt = vc_single(0, file_path, f0up_key, None, f0method, index_path, index_rate)
        except Exception as e:
            print("RVC_FAIL\t%s" % file, flush=True)
            print("  error converting %s: %r" % (file, e), flush=True)
            continue
        if wav_opt is not None:
            wavfile.write(os.path.join(opt_path, file), tgt_sr, wav_opt)
            print("RVC_DONE\t%s" % file, flush=True)
        else:
            print("RVC_FAIL\t%s" % file, flush=True)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
