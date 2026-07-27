"""Ported UPI payment-link / QR extraction core (from gpt-upi-tool).

Self-contained package: the only external entry point is ``run_upi_qr_probe``.
Login is delegated to the host project via a ``login_fn`` (see the UPI link
extractor adapter), so none of the upstream browser/mail login stack is pulled
in here.
"""
from __future__ import annotations

from .kakao_runner import KakaoError, run_kakao_link
from .upi_runner import UpiQrError, UpiQrResult, run_upi_qr_probe

__all__ = [
    "KakaoError",
    "run_kakao_link",
    "run_upi_qr_probe",
    "UpiQrResult",
    "UpiQrError",
]
