# Hailo-8 qualification: execution passed, model quality failed

On 2026-10-03 KST, an actual Hailo-8 completed four warmups and 100 ResNet-v1-18
inferences in a non-root Kubernetes Job admitted by Kueue. The fixed sample's
top-1 accuracy was **66%**, below the prospectively chosen **75%** threshold.
The variant remains **unqualified for recommendations**. No platform API
integration, PostgreSQL usage ingestion, S3 result delivery or MLflow delivery is
claimed for these direct qualification Jobs.

The [plan](hailo-qualification-plan.md) was published at commit `2fe4d4f` before
reference and model output inspection. [Raw measurements](evidence/hailo-qualification.json)
retain all 100 predictions and timings, plus both F0 attempts. The
[input manifest](evidence/hailo-input-manifest.json) retains the exact sample,
original reference predictions and tensor digests. This is assistant-executed
lab evidence, not a claim of manual operation by the repository author.

## Results and interpretation

| Measurement | Observed | Acceptance |
|---|---:|---|
| NPU top-1 label accuracy | 66/100 | Failed: minimum 75/100 |
| Original ONNX CPU reference accuracy | 64/100 | Reference, not a CPU performance comparison |
| NPU/reference top-1 agreement | 95/100 | Passed: minimum 90/100 |
| Absolute accuracy loss from reference | -0.02 | Passed: maximum +0.05 |
| Qualified | false | No registration or three follow-up API Jobs |
| Synchronous call latency p50 / p95 / p99 | 1.515 / 1.541 / 1.557 ms | Descriptive only |
| Throughput inside timed calls | 658.48 images/s | Single batch-one process |
| Host process peak RSS | 90.20 MiB | Host memory, not NPU memory |
| NPU utilization / memory / power | Unknown | No invented sensor measurements |

The reference also misses the absolute gate. This result does not isolate model
quality from dataset/preprocessing effects, establish a quantization improvement,
estimate full ImageNet accuracy, or justify lowering the threshold. A subsequent
model or input contract needs a new prospective evaluation and independent data.

Timing covers `InferVStreams.infer()` with an already prepared float32 host tensor
through synchronous NPU output. It excludes tensor conversion, input decoding,
network requests, queueing, model loading and report upload. p50 is the sample
median; p95/p99 use observations 95/99 in sorted order. There is only one measured
process; the report does not establish variance across independent Jobs.

## Allocation and failed preparation

| Attempt | Container exit | Outcome | Scheduled to container finish | Admission to Workload finish |
|---|---:|---|---:|---:|
| `ra-hailo-f0-01` | 1 | Image loader failure, no Python/device execution | 6 s | 17 s |
| `ra-hailo-f0-02` | 0 | Actual device open/identity succeeds | 6 s | 13 s |
| `ra-hailo-model-01` | 2 | All inference completed; quality gate rejects | 4 s | 9 s |
| Total | | Failed preparation included | **16 s** | **39 s** |

Each requested one `hailo.ai/h8`, one host CPU and 512 MiB host memory; no GPU.
These are distinct reservation/accounting boundaries, not physical utilization.
Timestamp resolution is one second. Downloads, image construction and reference
preparation are not included in these totals, and full preparation cost is not
yet measured. Workload finish includes controller observation delay.

The first runtime layer overlaid the base `/lib -> usr/lib` symlink with a
directory, hiding the ELF loader. Merging extracted `lib/` contents into `usr/lib/`
before constructing the layer fixed it. Both image digests and failed allocation
are retained. F0 initially warned about an unwritable Hailo cache; the model Job
uses a writable `/home/research` emptyDir as its container HOME.

After collection, the Hailo node was Ready with no memory/disk/PID pressure;
the dedicated queue had zero pending, admitted or reserving workloads. Existing
GPU quota and the host PCIe driver were unchanged. Actual device visibility
denial without allocation and over-quota NPU negative tests remain open.

## Reproduction

Use an ARM64 lab node with an already installed Hailo-8 PCIe driver 4.23.0 and
its existing device plugin. This procedure does not install or upgrade drivers.
HailoRT and firmware were 4.23.0; the container used Python 3.13.16 / NumPy 2.2.6.

1. Download the public assets below and verify their SHA-256 hashes. In a
   separate environment install `numpy==2.2.6`, `Pillow==11.3.0`, and
   `onnxruntime==1.22.1`. Run:

   ```sh
   python examples/prepare_hailo_fixture.py \
     --model-archive resnet_v1_18.zip --data-archive imagenette2-160.tgz \
     --output fixture
   ```

   This uses only the CPU for the original reference. Compare sample paths,
   tensor hashes and reference outputs to the published manifest. Manifest
   bytes include measured preparation durations, so a new manifest digest is
   expected; freeze and pass the newly computed digest to the model runner.

2. Build an isolated ARM64 image using the exact base and six package archives
   in [runtime-artifacts.json](../examples/hailo/runtime-artifacts.json).
   Verify archive hashes and extract with `dpkg-deb --extract` into a staging
   root; do not run host package installation or maintainer scripts. Merge any
   staged `lib/` into `usr/lib/`, then remove the staged `lib/` directory so
   the base symlink survives. Keep package license files. Install the hashed
   Python dependencies into the staging root:

   ```sh
   uv pip install --python-version 3.13 --python-platform aarch64-manylinux_2_28 \
     --only-binary :all: --require-hashes --target root/opt/python \
     -r examples/hailo/runtime-requirements.txt
   ```

   Create an OCI layer containing staged `etc/`, `opt/`, and `usr/`; append it
   with `crane mutate --platform linux/arm64 --append runtime.tar`. Set
   `PYTHONPATH=/opt/python:/usr/lib/python3/dist-packages`,
   `LD_LIBRARY_PATH=/usr/lib`, user `10001:10001`, workdir `/tmp`.
   The archive also contains GStreamer files; this experiment does not qualify
   GStreamer dependencies or video pipelines. Pydantic dependencies in the
   recorded image are unused by this qualification runner.

3. Add `src/resource_advisor/hailo_qualification.py` as
   `/opt/qualification/hailo_qualification.py`, and the frozen `inputs.npy`,
   `reference.npy`, `manifest.json` and matching `model.hef` under `/opt/fixture`.
   Pin the resulting image digest. Exact observed digests are recorded, but
   private registry images are not published; rebuilding may change the image
   digest. Record the rebuilt content hashes before submitting any experiment.

4. Create a dedicated project LocalQueue / ClusterQueue / ResourceFlavor using
   the existing node's label and `hailo.ai/h8` resource, with quotas CPU 1,
   memory 1 GiB, NPU 1. Bind its namespace selector to the lab namespace and
   do not borrow from the GPU queue. The observed cluster accepted Kueue
   `v1beta1`; choose a served API on your cluster without upgrading it.
   Adapt [job.json](../examples/hailo/job.json) with your namespace, queue,
   immutable image digest and freshly frozen manifest digest. It starts
   suspended for Kueue admission, has a 180-second cap and no retry.

5. Save Job, Pod, Workload and complete logs before cleanup. A line prefixed
   `RESOURCE_ADVISOR_HAILO_REPORT` contains the report. Exit 2 deliberately
   means model qualification failed even when every inference executed.
   Do not rerun until a pass, silently change preprocessing, or register a
   failed variant as recommendation-capable.

Public assets:

- [Original ONNX archive](https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/Classification/resnet_v1_18/pretrained/2022-04-19/resnet_v1_18.zip):
  `bc5776177ddad2b43f36c218ae06124b3dd67caaed9dd80c906c507f0c02a22f`
- [Hailo-8 HEF](https://hailo-model-zoo.s3.eu-west-2.amazonaws.com/ModelZoo/Compiled/v2.17.0/hailo8/resnet_v1_18.hef):
  `d5a76ed6f116fc9b9ef502df914a298917091724ed88cfccab36bfb716f61b22`
- [Imagenette2-160](https://s3.amazonaws.com/fast-ai-imageclas/imagenette2-160.tgz):
  `64d0c4859f35a461889e0147755a999a48b49bf38a7e0f9bd27003f10db02fe5`

The ten synsets map to zero-based 1,000-class outputs. Hailo's human-readable
ImageNet name file has a leading background entry, so name lookup uses index+1;
the model output/ground-truth indices themselves remain zero-based.
