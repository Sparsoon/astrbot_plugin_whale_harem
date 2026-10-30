# 上传 GitHub 前的准备清单

仓库根目录 = 本文件夹（`astrbot_plugin_whale_harem/`）。以下命令在**本文件夹内**执行。

## 1. 已帮你做好的

- `git init -b main` 已执行，`.gitignore` 已排除运行期数据（`data/`、`*.db`、缓存）；
- 目录名 = 插件名 = `astrbot_plugin_whale_harem`（AstrBot 直接识别）；
- 敏感信息已清理：仓库内没有服务器 IP、没有密码、部署脚本不在仓库里（在上级 `util/`）。

## 2. 建远程仓库并推送

在 GitHub 网页新建 **空仓库**（不要勾选 README / .gitignore / License），名字建议：

```
astrbot_plugin_whale_harem
```

然后在本目录执行（把 `<你的用户名>` 换掉）：

```bash
git add .
git commit -m "feat: 赛博鲸鱼馆 v1.5.1（钓鱼/天气/喂食/打工/花园）"
git remote add origin https://github.com/<你的用户名>/astrbot_plugin_whale_harem.git
git push -u origin main
```

## 3. 仓库资料建议

- **Description**：`AstrBot 群玩插件：共享池塘钓鱼、随机天气、签到喂食打工、娶群友、养植物，全部 Pillow 出图`
- **Topics**：`astrbot` `astrbot-plugin` `qqbot` `pillow` `sqlite` `game`
- **About → Website**：可留空
- 首次 push 后把 `metadata.yaml` 里的 `repo:` 改成真实地址再提交一次

## 4. 发布 Release（可选）

打 tag 即可让 AstrBot 插件市场识别版本：

```bash
git tag v1.5.1
git push origin v1.5.1
```

## 5. 上传前最后检查

```bash
git status --short          # 应只剩你刚改的文件
git ls-files | grep -E "\.db$|__pycache__|data/"   # 应该没有输出
```

确认 `README.md` 顶部的版本徽章、`metadata.yaml` 的 version 都是 `1.4.0`。
