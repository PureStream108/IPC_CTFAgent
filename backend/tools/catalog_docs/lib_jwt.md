# PyJWT / jwcrypto

JWT 编解码、签名校验、JWS/JWE 与 JWK 操作。

## 用途与适用场景

需要解析 JWT header/payload、验证 HS/RS/ES 签名、测试 alg/kid 混淆，或处理 JWS/JWE/JWK 时使用。

## 版本检查

```bash
python3 -c "import jwt, jwcrypto; print(jwt.__version__, jwcrypto.__version__)"
```

## 命令、导入与镜像路径

- 常用入口：`python3`
- 镜像路径：`Python site-packages（PyJWT、jwcrypto）`

## 常用工作流

先 base64url 解码 header/payload，确认 alg、kid、jwk；用 PyJWT 的 `decode(..., algorithms=[...])` 验证签名，用 jwcrypto 处理 JWS/JWE。测试 alg=none、弱 HMAC secret、kid 路径注入和 JWK 混淆。

## 可执行示例

```bash
python3 -c "import jwt; t=jwt.encode({'sub':'alice'}, 'secret', algorithm='HS256'); print(jwt.decode(t, 'secret', algorithms=['HS256']))"
```

## 输出解释

观察 header 的 alg/kid、payload 的声明、签名校验异常和 JWK 字段；成功解码不等于授权通过。

## 常见错误与限制

不要在生产代码中省略 algorithms 或关闭签名校验。jwcrypto 的 API 与 PyJWT 不同，JWE 需要正确选择算法和密钥。

## 关联条目

- [Requests / Flask / PyJWT / jwcrypto / Jinja2](/memory/catalog/lib_web_python/document)

## 官方参考

- [https://pyjwt.readthedocs.io/](https://pyjwt.readthedocs.io/)
