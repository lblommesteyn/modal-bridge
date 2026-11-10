"""Text-to-image on a Modal GPU, shaped for Modal Bridge: one JSON argument in, JSON out.

    modal deploy examples/image-gen/modal_app.py

SDXL-Turbo is released under Stability AI's community license; read it before
commercial use, or swap MODEL for one whose terms suit you.
"""
import base64
import io
import os
import time

import modal

MODEL = "stabilityai/sdxl-turbo"
# IMAGE_GEN_GPU=T4 picks another GPU. IMAGE_GEN_GPU=none runs on CPU: slow (tens of
# seconds per image) but enough to try the whole flow on an account without GPU access.
GPU = os.environ.get("IMAGE_GEN_GPU", "L4")
ON_CPU = GPU.lower() == "none"
app = modal.App("modal-bridge-image-gen")


def download():
    import torch
    from diffusers import AutoPipelineForText2Image
    AutoPipelineForText2Image.from_pretrained(MODEL, torch_dtype=torch.float16, variant="fp16")


image = (modal.Image.debian_slim(python_version="3.12")
         .pip_install("torch>=2.4,<3", "diffusers>=0.30,<1", "transformers>=4.44,<5",
                      "accelerate>=0.33", "safetensors>=0.4", "pillow>=10")
         .run_function(download)  # Weights are baked into the image, so cold starts skip the download.
         .env({"IMAGE_GEN_GPU": GPU}))

pipeline = None


# max_containers=1 caps spend: at most one GPU runs however many requests arrive.
@app.function(image=image, timeout=600 if ON_CPU else 180, max_containers=1, scaledown_window=120,
              **(dict(cpu=8, memory=20480) if ON_CPU else dict(gpu=GPU)))
def generate(payload: dict) -> dict:
    global pipeline
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 300:
        raise ValueError("prompt must be 1-300 characters")
    steps = payload.get("steps", 4)
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 4:
        raise ValueError("steps must be an integer from 1 to 4")
    seed = payload.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2 ** 32):
        raise ValueError("seed must be an integer below 2**32")

    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if pipeline is None:
        from diffusers import AutoPipelineForText2Image
        pipeline = AutoPipelineForText2Image.from_pretrained(MODEL, torch_dtype=torch.float16, variant="fp16")
        pipeline = pipeline.to("cuda") if device == "cuda" else pipeline.to("cpu", torch.float32)
    if seed is None:
        seed = int.from_bytes(torch.randint(0, 256, (4,), dtype=torch.uint8).numpy().tobytes(), "little")
    started = time.perf_counter()
    picture = pipeline(prompt=prompt.strip(), num_inference_steps=steps, guidance_scale=0.0,
                       generator=torch.Generator(device).manual_seed(seed)).images[0]
    gpu_seconds = round(time.perf_counter() - started, 3)  # Inference time; on CPU when no GPU is attached.

    buffer = io.BytesIO()
    picture.save(buffer, format="JPEG", quality=90)
    # ~50-100 KB, well inside the gateway's 1 MiB result limit. For larger artifacts
    # (video, batches) upload from here to object storage and return a reference.
    return {"image_base64": base64.b64encode(buffer.getvalue()).decode(), "content_type": "image/jpeg",
            "width": picture.width, "height": picture.height, "prompt": prompt.strip(),
            "seed": seed, "steps": steps, "gpu_seconds": gpu_seconds, "device": device, "model": MODEL}
