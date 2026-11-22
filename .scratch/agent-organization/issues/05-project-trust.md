# 05: 项目信任（Project Trust）基础

**What to build:** app/agents/project_trust.py + catalog.projects 表：项目登记
（project_id/name/root_path/canonical_root/trust_state）；root 规范化（os.path.realpath 风格，
Windows 大小写折叠），拒绝 symlink 逃逸/`..`/盘符切换/UNC；Project Root 与 Task Workspace
概念分离（任务 workspace 独立，project_id 只是身份与发现域）。未信任项目不可激活其
项目级 agent。撤销信任只影响新任务，历史任务记录保留。

**Blocked by:** 02

**Status:** done

- [x] 项目登记/规范化/逃逸校验
- [x] 信任状态迁移（pending→trusted→revoked）
- [x] 测试（含逃逸向量）并提交
