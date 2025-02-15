import argparse
import collections
import copy
import json

from surya.benchmark.bbox import get_pdf_lines
from surya.benchmark.metrics import precision_recall
from surya.benchmark.tesseract import tesseract_bboxes, tesseract_parallel
from surya.model.segformer import load_model, load_processor
from surya.model.processing import open_pdf, get_page_images
from surya.detection import batch_inference
from surya.postprocessing.heatmap import draw_bboxes_on_image
from surya.postprocessing.util import rescale_bbox
from surya.settings import settings
import os
import time
from tabulate import tabulate
import datasets


def main():
    parser = argparse.ArgumentParser(description="Detect bboxes in a PDF.")
    parser.add_argument("--pdf_path", type=str, help="Path to PDF to detect bboxes in.", default=None)
    parser.add_argument("--results_dir", type=str, help="Path to JSON file with OCR results.", default=os.path.join(settings.RESULT_DIR, "benchmark"))
    parser.add_argument("--max", type=int, help="Maximum number of pdf pages to OCR.", default=100)
    parser.add_argument("--debug", action="store_true", help="Run in debug mode.", default=False)
    args = parser.parse_args()

    model = load_model()
    processor = load_processor()

    if args.pdf_path is not None:
        pathname = args.pdf_path
        doc = open_pdf(args.pdf_path)
        page_count = len(doc)
        page_indices = list(range(page_count))
        page_indices = page_indices[:args.max]

        images = get_page_images(doc, page_indices)
        doc.close()

        image_sizes = [img.size for img in images]
        correct_boxes = get_pdf_lines(args.pdf_path, image_sizes)
    else:
        pathname = "doclaynet_bench"
        # These have already been shuffled randomly, so sampling from the start is fine
        dataset = datasets.load_dataset(settings.BENCH_DATASET_NAME, split=f"train[:{args.max}]")
        images = list(dataset["image"])
        images = [i.convert("RGB") for i in images]
        correct_boxes = []
        for i, boxes in enumerate(dataset["bboxes"]):
            img_size = images[i].size
            # 1000,1000 is bbox size for doclaynet
            correct_boxes.append([rescale_bbox(b, (1000, 1000), img_size) for b in boxes])

    start = time.time()
    predictions = batch_inference(images, model, processor)
    surya_time = time.time() - start

    start = time.time()
    tess_predictions = tesseract_parallel(images)
    tess_time = time.time() - start

    folder_name = os.path.basename(pathname).split(".")[0]
    result_path = os.path.join(args.results_dir, folder_name)
    os.makedirs(result_path, exist_ok=True)

    page_metrics = collections.OrderedDict()
    for idx, (tb, sb, cb) in enumerate(zip(tess_predictions, predictions, correct_boxes)):
        surya_boxes = sb["bboxes"]

        surya_metrics = precision_recall(surya_boxes, cb)
        tess_metrics = precision_recall(tb, cb)

        page_metrics[idx] = {
            "surya": surya_metrics,
            "tesseract": tess_metrics
        }

        if args.debug:
            bbox_image = draw_bboxes_on_image(surya_boxes, copy.deepcopy(images[idx]))
            bbox_image.save(os.path.join(result_path, f"{idx}_bbox.png"))

    mean_metrics = {}
    metric_types = sorted(page_metrics[0]["surya"].keys())
    for k in ["surya", "tesseract"]:
        for m in metric_types:
            metric = []
            for page in page_metrics:
                metric.append(page_metrics[page][k][m])
            if k not in mean_metrics:
                mean_metrics[k] = {}
            mean_metrics[k][m] = sum(metric) / len(metric)

    out_data = {
        "times": {
            "surya": surya_time,
            "tesseract": tess_time
        },
        "metrics": mean_metrics,
        "page_metrics": page_metrics
    }

    with open(os.path.join(result_path, "results.json"), "w+") as f:
        json.dump(out_data, f, indent=4)

    table_headers = ["Model", "Time (s)", "Time per page (s)"] + metric_types
    table_data = [
        ["surya", surya_time, surya_time / len(images)] + [mean_metrics["surya"][m] for m in metric_types],
        ["tesseract", tess_time, tess_time / len(images)] + [mean_metrics["tesseract"][m] for m in metric_types]
    ]

    print(tabulate(table_data, headers=table_headers, tablefmt="github"))
    print("Precision and recall are over the mutual coverage of the detected boxes and the ground truth boxes at a .5 threshold.  There is a precision penalty for multiple boxes overlapping reference lines.")
    print(f"Wrote results to {result_path}")


if __name__ == "__main__":
    main()
