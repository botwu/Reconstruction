# 原生文件搜索运行依赖

ripgrep 14.1.1 来自 BurntSushi/ripgrep 官方固定发行包，未经修改。
来源、上游发布的压缩包 SHA256、展开后 rg 的 SHA256 固定在 ../ripgrep_lock.json；
原 MIT / Unlicense 双许可随包保留。

当前 AGS node-python-hermes 模板没有 rg，Hermes 的 grep 回退不支持相同的正则或运算，
真实 search_files 因此返回错误的零匹配。GatewayHermesAgent.setup 离线上传并核验本包，
将 rg 安装到该次临时 AGS 沙箱已有 login-shell PATH 中的 /usr/local/bin。
不改宿主、预置模板、第三方工具实现或任务材料；沙箱销毁后该安装一并回收。仅支持实际使用的 Linux x86_64；
平台、哈希或版本不匹配时 setup 明确失败。
