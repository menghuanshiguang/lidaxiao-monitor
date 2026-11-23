"""OCR 引擎工厂: RapidOCR 单例 (DirectML 优先, 不可用回退 CPU) — 从 monitor.py 抽出"""
from lidaxiao.textutil import log

_ocr = None


def get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        # 优先 DirectML (RTX 4060 GPU 加速); 不可用时自动回退 CPU
        try:
            _ocr = RapidOCR(intra_op_num_threads=8,
                            det_use_dml=True, cls_use_dml=True, rec_use_dml=True)
            if _ocr.text_det.infer.session.get_providers()[0] != "DmlExecutionProvider":
                raise RuntimeError("DML provider not active")
        except Exception:
            log("DirectML 不可用, 回退 CPU OCR")
            _ocr = RapidOCR(intra_op_num_threads=8)
    return _ocr
