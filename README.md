# App Store Publisher 🚀

自动将 APK 发布到中国主流安卓应用商店的 CLI 工具。

## 支持的应用商店

| 商店 | 标识 | APK 文件后缀 |
|------|------|-------------|
| 腾讯应用宝 | `yingyongbao` | `*-yingyongbao.apk` / `*-tencent.apk` / `*-qq.apk` |
| 华为应用市场 | `huawei` | `*-huawei.apk` |
| 荣耀应用市场 | `honor` | `*-honor.apk` |
| vivo 应用商店 | `vivo` | `*-vivo.apk` |
| OPPO 应用商店 | `oppo` | `*-oppo.apk` |
| 小米应用商店 | `xiaomi` | `*-xiaomi.apk` |

## 安装

```bash
pip install -e .
```

## 配置

```bash
cp config.example.toml config.toml
# 编辑 config.toml 填入各商店的凭据
```

## 使用

```bash
# 发布当前目录下所有渠道 APK
appstore-publisher publish ./release-*.apk

# 发布指定文件
appstore-publisher publish release-vivo.apk release-oppo.apk

# 预览模式（只检测渠道，不上传）
appstore-publisher publish --dry-run ./release-*.apk

# 使用自定义配置
appstore-publisher -c myconfig.toml publish ./apks/

# 详细模式
appstore-publisher -v publish ./release-*.apk

# 查看支持的渠道
appstore-publisher channels
```

## 项目结构

```
src/appstore_publisher/
├── cli.py              # CLI 入口 (Click)
├── config.py           # 配置加载与验证
├── models.py           # 数据模型
├── publisher.py        # 发布流程编排
├── channel_detector.py # 文件名渠道检测
├── utils.py            # 签名/哈希工具
└── stores/
    ├── base.py         # 抽象基类
    ├── yingyongbao.py  # 腾讯应用宝
    ├── huawei.py       # 华为
    ├── honor.py        # 荣耀
    ├── vivo.py         # vivo
    ├── oppo.py         # OPPO
    └── xiaomi.py       # 小米
```

## License

MIT
