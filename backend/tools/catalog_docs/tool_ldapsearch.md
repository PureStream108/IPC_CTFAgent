# ldapsearch

查询 LDAP 目录的 OpenLDAP 客户端。

## 用途与适用场景

枚举 naming context、schema 与目录条目；域环境里常能直接读出用户、组与描述字段里的密码提示。

## 版本检查

```bash
ldapsearch -VV
```

## 命令、导入与镜像路径

- 常用入口：`ldapsearch`
- 镜像路径：`/usr/bin/ldapsearch（ldap-utils 包）`

## 常用工作流

先匿名搜索 base 与 subschema，再根据结果构造 (objectClass=*) 过滤；需要认证时用 -D/-w（或 -W）绑定。

## 可执行示例

```bash
ldapsearch -x -H ldap://TARGET:389 -s base namingContexts
```

## 输出解释

LDIF 文本：dn 与属性列表；result: 0 Success 才表示查询成功，result: 49 表示凭据错误。

## 常见错误与限制

匿名绑定在多数生产目录被禁用；大批量枚举会触发审计告警，应先用 -z 限制返回条数。

## 关联条目

- [enum4linux](/memory/catalog/tool_enum4linux/document)
- [nmap](/memory/catalog/tool_nmap/document)

## 官方参考

- [https://www.openldap.org/software/man.cgi?query=ldapsearch](https://www.openldap.org/software/man.cgi?query=ldapsearch)
