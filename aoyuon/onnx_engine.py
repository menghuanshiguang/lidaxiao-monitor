"""ONNX Runtime 推理封装 —— recognize.py 和 eval.py 共用, 只此一处.

设计要点:
- 单线程 inter-op, 可配置 intra-op (=4 对应验收标准的 4 核);
- 输入固定 1 通道灰度, 高 48, 宽动态 (训练时按 batch 内最大宽补齐);
- 不做 session 复用之外的任何全局状态, 实例可反复创建。
"""
import numpy as np

PROVIDER = "CPUExecutionProvider"


class OnnxCRNN:
    def __init__(self, model_path, threads=4, optimize=True):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = int(threads)
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        if optimize:
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        avail = ort.get_available_providers()
        if PROVIDER not in avail:
            raise RuntimeError(f"onnxruntime 没有 {PROVIDER}, 只有 {avail}")
        self.sess = ort.InferenceSession(model_path, so, providers=[PROVIDER])
        self.in_name = self.sess.get_inputs()[0].name
        self.out_name = self.sess.get_outputs()[0].name
        self.model_path = model_path

    def logits(self, x):
        """x: (1, 48, W) float32 -> logits (T, n_cls) float32"""
        x = np.ascontiguousarray(x, dtype=np.float32)
        if x.ndim == 2:
            x = x[None]
        out = self.sess.run([self.out_name], {self.in_name: x[None]})[0]
        # 导出时输出是 (T, batch, n_cls)
        if out.ndim == 3:
            out = out[:, 0, :]
        return out
