# SSH 主机密钥变更

## 主机身份

演示指南：SSH 出现 REMOTE HOST IDENTIFICATION HAS CHANGED 表示已保存的主机身份与当前不同，不等于用户自己的 SSH 私钥失效。先确认实际访问的主机与网络。

## 可信核验

通过管理员或服务官方渠道核对主机指纹，再按批准步骤更新 known_hosts。禁止盲目删除全部记录或使用 StrictHostKeyChecking=no 绕过核验。
