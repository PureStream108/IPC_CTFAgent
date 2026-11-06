# Requests / Flask / PyJWT / jwcrypto / Jinja2

用于 HTTP 请求、本地 Web 复现、JWT/JWS/JWE 分析和模板注入研究。

## 用途与适用场景

需要脚本化请求、搭建本地 Flask 复现环境、解析或伪造 JWT、验证 JWK/JWE，或测试 Jinja2 模板行为时使用。

## 版本检查

```bash
python3 -c "import requests,flask,jwt,jwcrypto,jinja2; print(requests.__version__, flask.__version__, jwt.__version__)"
```

## 命令、导入与镜像路径

- 常用入口：`python3`
- 镜像路径：`Python site-packages（requests、flask、PyJWT、jwcrypto、jinja2）`

## 常用工作流

先用 requests 固定 session、cookies 和 headers 复现请求；JWT 题目先 base64 解码 header/payload，再验证 alg、kid、JWK 与签名。jwcrypto 用于 JWS/JWE，PyJWT 用于常规 JWT 编解码。模板题在本地起 Flask 验证 payload，再发给目标。

## 可执行示例

```bash
python3 -c "import jwt; t=jwt.encode({'sub':'alice'}, 'secret', algorithm='HS256'); print(t); print(jwt.decode(t, 'secret', algorithms=['HS256']))"
```

## 输出解释

检查 HTTP 状态码、响应头、Set-Cookie、JWT header/payload 和签名校验异常；模板渲染结果要与本地复现一致。

## 常见错误与限制

`jwt.decode` 默认要求显式 algorithms；不要关闭签名校验后把结果用于授权。jwcrypto 与 PyJWT 的 API 不同，先看版本与文档。

## 关联条目

- [PyJWT / jwcrypto](/memory/catalog/lib_jwt/document)
- [flask-pin](/memory/catalog/tool_flask_pin/document)

## 官方参考

- [https://pyjwt.readthedocs.io/](https://pyjwt.readthedocs.io/)
