# Worktree Isolation 实现逻辑

本文说明并行 Agent 如何通过 Git worktree 获得目录级隔离。主要实现位于 `s18_worktree_isolation/code.py`，综合实现位于 `s20_comprehensive/code.py`。

## 1. 隔离对象

Task System 解决“谁负责什么”，Worktree 解决“在哪里修改”：

```text
Task
  -> 目标、依赖、owner、状态

Worktree
  -> 独立目录、独立分支、独立文件修改
```

只有任务分工，没有目录隔离时，多个队友仍可能覆盖同一工作区中的修改。

## 2. 目录与分支

教学实现使用：

```text
.worktrees/<name>/
branch: wt/<name>
```

`WORKTREES_DIR` 位于当前仓库内。每个 worktree 仍共享 Git object database，但拥有独立工作目录和索引。

## 3. 名称校验

`validate_worktree_name(name)`：

- 拒绝空名称。
- 拒绝 `.` 和 `..`。
- 限制长度。
- 只允许字母、数字、点、下划线和连字符。

名称同时进入路径和分支名，因此必须先校验，不能直接把模型输入拼进 `git worktree add`。

## 4. Git 命令封装

`run_git(args)` 使用参数数组调用 Git：

```python
subprocess.run(
    ["git", *args],
    cwd=WORKDIR,
    ...
)
```

它返回 `(success, output)`。上层只有在命令成功后才更新 task 绑定或写事件日志。

参数数组比拼接 shell 字符串更容易控制路径和特殊字符。

## 5. 创建 Worktree

`create_worktree(name, task_id="")`：

```text
validate name
  -> 检查目标目录
  -> git worktree add <path> -b wt/<name> HEAD
  -> 可选绑定 task
  -> 写 create 事件
```

绑定发生在 Git 创建成功之后，避免任务记录指向不存在的目录。

## 6. Task 绑定

Task 数据增加 `worktree` 字段。

`bind_task_to_worktree()` 只更新这个字段：

```text
task.worktree = name
status 保持 pending
owner 保持为空
```

创建目录不等于认领任务。Lead 可以先建立任务和工作区，队友稍后通过正常 claim 流程获取它。

## 7. 队友 cwd 切换

每个 teammate 线程维护自己的 worktree context：

```python
wt_ctx = {"path": None}
```

claim 成功后：

```text
load task
  -> task.worktree 存在
  -> wt_ctx.path = .worktrees/<name>
```

队友的基础工具调用把该路径作为 cwd：

```text
bash(command, cwd=wt_ctx.path)
read_file(path, cwd=wt_ctx.path)
write_file(path, cwd=wt_ctx.path)
```

主 Agent 的 cwd 不需要改变。目录选择属于该 teammate 的执行上下文。

## 8. safe_path 与 cwd

支持 worktree 后，路径安全检查不能永远以仓库根目录为基准。

正确边界是：

```text
当前 Agent 的执行根目录
  -> WORKDIR
  或
  -> 已分配 worktree
```

`safe_path(p, cwd)` 将相对路径解析到当前根目录，并检查结果仍位于该根目录内。

## 9. 修改检测

删除前 `_count_worktree_changes(path)` 检查：

- 未提交文件变化。
- 相对基线新增的提交。

如果存在改动或提交，默认拒绝删除：

```text
remove_worktree(name)
  -> dirty：拒绝
  -> clean：删除
```

必须显式传入 `discard_changes=True` 才允许强制删除有修改的 worktree。

## 10. Keep 与 Remove

### keep_worktree

保留目录和分支，供人工 review、测试和合并。它只记录事件，不自动 merge。

### remove_worktree

执行：

```text
git worktree remove --force
  -> git branch -D wt/<name>
  -> 写 remove 事件
```

删除工作区不自动 complete task。任务状态仍由 `complete_task()` 管理，避免把目录生命周期和业务完成状态混为一谈。

## 11. 生命周期日志

`.worktrees/events.jsonl` 记录：

```json
{
  "type": "create",
  "worktree": "auth-refactor",
  "task_id": "1",
  "ts": 1234567890
}
```

事件包括 create、keep、remove。日志用于审计和排查，不是完整状态数据库。

真实状态仍应以：

```text
git worktree list
文件系统
task.worktree
```

综合判断。

## 12. 与 Autonomous Agent 的连接

队友自动认领带 worktree 的任务后，立即切换工具 cwd：

```text
scan task board
  -> claim
  -> read task.worktree
  -> bind cwd
  -> execute tools
```

这样自治调度和目录隔离形成闭环。

## 13. 与权限系统的连接

Worktree 不是权限沙箱。bash 仍可能访问绝对路径或仓库外资源。

目录隔离负责降低并行修改冲突，Permission 与 OS sandbox 负责限制危险操作，两者不可互相替代。

## 14. 当前实现边界

- 创建基于当前 `HEAD`，没有远程默认分支同步策略。
- branch 名冲突时创建失败。
- task 与 worktree 是一对一的教学模型。
- 没有自动 merge、rebase 和冲突解决。
- teammate 的 cwd 存在于线程内存，进程重启后需恢复。
- events.jsonl 没有锁和状态重放逻辑。
- 强制删除依赖模型或用户正确设置 `discard_changes`。
- Windows、长路径和 Git 配置差异可能影响命令结果。

## 15. 总结

Worktree Isolation 的关键链路是：

```text
创建独立目录和分支
  -> 绑定 task
  -> claim 后切换 teammate cwd
  -> 独立修改
  -> keep review 或安全 remove
```

它把并行 Agent 的隔离从“各自有独立 messages”扩展到“各自有独立文件系统视图”。
