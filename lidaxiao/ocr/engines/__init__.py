"""OCR 引擎实现: aoyuon(本地权重) / xiaomi(llama-server 常驻)

这里刻意不做 re-export: 两个引擎顶层分别 import numpy / urllib, 一旦在包入口
同时导入, 只用一个引擎也会把另一个的依赖拖进来 (本机没 numpy, 直接 ImportError)。
按需 `from lidaxiao.ocr.engines.xiaomi import XiaomiOCR` 取用。
"""
