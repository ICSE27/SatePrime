import time
from datetime import datetime

print(f"[{datetime.now().strftime('%H:%M:%S')}] [INFO] 正在初始化依赖库...", flush=True)
_import_start = time.time()
_program_start = time.time()
_program_status = "SUCCESS"

import torch

from pyraws.utils.database_utils import get_event_info, get_events_list
from pyraws.utils.l1_utils import read_L1C_image_from_tif

_import_end = time.time()
print(
    f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] Import加载完成，"
    f"耗时: {_import_end - _import_start:.2f} 秒\n",
    flush=True,
)


def main():
    event_id = "Greece_5"
    out_name_ending = "2"
    database = "THRAWS"

    events = get_events_list(database)
    print(f"database: {database}")
    print(f"events_count: {len(events)}")
    print(f"event_id: {event_id}")

    (
        raw_img_path,
        l1_img_path,
        l1c_post_processed_path,
        expected_class,
        raw_useful_granules,
        raw_complementary_granules,
        coords,
        requested_polygon,
    ) = get_event_info(event_id, database=database)

    print(f"expected_class: {expected_class}")
    print(f"raw_path: {raw_img_path}")
    print(f"l1c_path: {l1_img_path}")
    print(f"cropped_tif_base: {l1c_post_processed_path}")
    print(f"raw_useful_granules: {raw_useful_granules}")
    print(f"raw_complementary_granules: {raw_complementary_granules}")
    print(f"event_coords: {coords}")
    print(f"requested_polygon_points: {len(requested_polygon)}")

    read_start = time.time()
    image, coords_dict, image_class = read_L1C_image_from_tif(
        event_id,
        out_name_ending=out_name_ending,
        database=database,
        device=torch.device("cpu"),
    )
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] [SUCCESS] TIF读取完成，"
        f"耗时: {time.time() - read_start:.2f} 秒"
    )
    print(f"image_shape: {tuple(image.shape)}")
    print(f"image_dtype: {image.dtype}")
    print(f"image_min: {float(image.min()):.6f}")
    print(f"image_max: {float(image.max()):.6f}")
    print(f"image_class: {image_class}")
    print(f"lat_shape: {coords_dict['lat'].shape}")
    print(f"lon_shape: {coords_dict['lon'].shape}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _program_status = "ERROR"
        raise
    finally:
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] [{_program_status}] 程序结束，"
            f"总耗时: {time.time() - _program_start:.2f} 秒",
            flush=True,
        )
