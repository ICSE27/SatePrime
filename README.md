# SatePrime: Performance Optimization of Satellite Applications in Satellite Computing

## Evaluated Satellite Applications

 - The evaluation suite comprises 10 typical satellite on-orbit applications, labeled app1 to app10, under the `apps/` directory.
 - Each application is accompanied by a Dockerfile tailored for the satellite payload environment, combining its code repository link.

    | AppID | Description |
    | ----- | ----------- |
    | App1  | Object Detection |
    | App2  | Image Classification |
    | App3  | Data Compression |
    | App4  | Semantic Segmentation |
    | App5  | Oriented Object Detection |
    | App6  | Rotated Object Detection |
    | App7  | Onboard Inference |
    | App8  | Cloud Detection |
    | App9  | Target Detection |
    | App10 | Model Deployment |

## Satellite Application Performance Optimization Code

SatePrime implements a performance optimization framework for containerized satellite applications through two phases, located in the `satecode/` directory.

The Ground-Side Construction Phase builds an optimized and recoverable container image. It profiles application startup, preloads dependencies, and checkpoints the process at a stable recovery point before input-specific computation (Step 1). It then replays recovery with I/O tracing to identify startup file access order and separates files into foreground and background tiers (Step 2). Finally, it merges the root file system with the checkpointed state, relays out files by access order, and packs them into a read-only EROFS image with an embedded heat boundary (Step 3).
The main implementation is provided in the files `agent.py`, `runtime.py`, and `monitor.py`.

The Satellite-Side Execution Phase accelerates and safeguards startup. It prefetches heat-boundary bytes to warm the page cache, restores the application from the recovery point, and resumes execution directly (Step 4). It also checks recovery throughout restoration and falls back to the original cold-start path upon failure (Step 5).
The main implementation is provided in the file `codegen.py`.

Other code
 - We provide a command-line entry point that exposes the above stages as composable subcommands (`patch`, `snapshot`, `record`, `build`, `emit`). (`cli.py` and `__main__.py`)
 - We provide shared configuration for image labels, environment keys, and file paths used across the pipeline. (`config.py`)


## Deployment

- Assets related to a deployment case are provided under the `deployment/` directory.
