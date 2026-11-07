"""统一异常类型，CLI 据此决定提示文案和退出码。"""

from __future__ import annotations


class HeiboxError(Exception):
    """所有可预期错误的基类。"""


class ConfigError(HeiboxError):
    """配置缺失，或数据库与当前配置不是同一个用户。"""


class NetworkError(HeiboxError):
    """可重试的失败：超时、5xx、429。"""


class LoginError(HeiboxError):
    """扫码登录失败：二维码过期、超时、未拿到 Cookie。"""


class CaptchaRequired(HeiboxError):
    """接口要求人机验证（status=show_captcha），需要用户先完成验证或等待冷却。"""


class ApiError(HeiboxError):
    """接口明确返回失败，或 HTTP 4xx（403 等），不重试。"""


class ApiSchemaError(ApiError):
    """响应结构变化，通常意味着接口改版，需要升级程序。"""
