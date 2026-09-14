#!/usr/bin/env bash
# commit 消息校验规则本体，本地钩子与 CI 共用同一套：
#   本地（commit-msg 钩子）：commit_msg_check.sh <消息文件> --cached
#   CI（逐条已有提交）：    commit_msg_check.sh <消息文件> --commit <sha>
# 两种调用只在「gitlink 变动从哪取」「怎么判合并提交」上分叉，规则本身不分叉：
#   --cached → 暂存区 diff-index --cached（首次提交对空树）；MERGE_HEAD 判合并
#   --commit → 该提交 diff-tree -r --root 对其父；<sha>^2 存在判合并
set -euo pipefail

msg_file="$1"
mode="${2:---cached}"
sha="${3:-}"
if [ "$mode" != "--cached" ] && { [ "$mode" != "--commit" ] || [ -z "$sha" ]; }; then
    echo "用法：commit_msg_check.sh <消息文件> --cached | --commit <sha>" >&2
    exit 2
fi

pattern='claude|cursor|codex|copilot|chatgpt|gemini|codeium|windsurf|aider|devin|tabnine|cody'

if grep -qiE "$pattern" "$msg_file"; then
    echo "✗ commit 消息未通过校验，请修改措辞后重试。" >&2
    echo "  紧急情况可用 git commit --no-verify 跳过（不推荐）。" >&2
    exit 1
fi

# 合并提交豁免下面全部标题结构检查
if [ "$mode" = "--commit" ]; then
    git rev-parse -q --verify "${sha}^2" >/dev/null 2>&1 && exit 0
else
    [ -e "$(git rev-parse --git-path MERGE_HEAD)" ] && exit 0
fi

title="$(awk '!/^#/ && NF { print; exit }' "$msg_file")"

# ---------- 类型前缀白名单（所有提交） ----------
# Conventional Commits 的收紧实现：类型全小写（大小写敏感，规范建议团队自定，
# 本仓选全小写）；可带 (范围)，范围内 / \ , 等分隔符均可；破坏性变更在冒号前
# 加 !（feat!: 或 feat(api)!:）；冒号必须半角 : 且后面恰好一个空格；描述非空。
types='feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert'
scope='(\([^)]+\))?'
type_head="^(${types})${scope}!?: "
if ! [[ "$title" =~ ${type_head}[^[:space:]] ]]; then
    {
        echo "✗ 标题须为「类型: 描述」或「类型(范围): 描述」，类型限定（全小写，大小写敏感）："
        echo "  feat fix docs style refactor perf test build ci chore revert"
        echo "  冒号用半角 : 且后面恰好一个空格；破坏性变更在冒号前加 !（如 feat!: 或 feat(api)!:）。"
        echo "  当前标题：$title"
        echo "  紧急情况可用 git commit --no-verify 跳过（不推荐）。"
    } >&2
    exit 1
fi

# ---------- ! 破坏性标记：须配套 BREAKING CHANGE 说明（官方规则） ----------
bang_head="^(${types})${scope}!: "
if [[ "$title" =~ $bang_head ]] && \
   ! grep -qE '^BREAKING[- ]CHANGE: [^[:space:]]' "$msg_file"; then
    {
        echo "✗ 标题带 ! 声明破坏性变更，正文或脚注须配套「BREAKING CHANGE: 说明」。"
        echo "  官方格式（脚注顶行写，冒号后一个空格）："
        echo "    chore!: 移除旧配置读取"
        echo ""
        echo "    BREAKING CHANGE: 不再兼容 .flopsrc，需迁移到新格式"
        echo "  紧急情况可用 git commit --no-verify 跳过（不推荐）。"
    } >&2
    exit 1
fi

# ---------- bump 专用强校验：只咬「自称 bump」的标题 ----------
# 类型头（与上面同一形态，含可选 ! ）后紧跟 bump 才触发；有 gitlink 变动但标题
# 不提 bump 的混合提交（开发进展顺带动了子模块指针）在这里直接放行，不做任何
# bump 格式检查。
bump_re="${type_head}[Bb][Uu][Mm][Pp]([^[:alnum:]]|$)"
[[ "$title" =~ $bump_re ]] || exit 0

# 自称 bump 的提交要求：
# 1) 本次提交确实有 gitlink（mode 160000）指针变动（删除子模块不算 bump）；
#    纯版本号发布（只改 package.json 之类）不许用 bump 措辞，改用 chore(release) 等。
# 2) 每个变动子模块写明「名字 → 新短SHA」（名字取路径尾段，带目录层级的可写
#    「父目录 空格 尾段」，如 FlopsWeb cocoder-ui-core），短SHA须是该子模块新指针的
#    真实前缀（≥7位）；标题末尾用括号说明本次子仓更新内容，多个子模块共用一份括号。

# gitlink 变动来源：--cached 取暂存区（diff-index 天然递归展开全路径）；
# --commit 取该提交对父的 diff-tree，须带 -r 才能看到子目录里的 gitlink，
# --root 兜住首次提交（对空树），--no-commit-id 去掉首行提交号。
gitlink_changes() {
    if [ "$mode" = "--commit" ]; then
        git diff-tree -r --no-commit-id --root -z --raw "$sha"
    else
        local base
        if git rev-parse -q --verify HEAD >/dev/null 2>&1; then
            base=HEAD
        else
            base=$(git hash-object -t tree /dev/null)   # 首次提交：与空树对比
        fi
        git diff-index --cached -z "$base"
    fi
}

example='chore: bump FlopsDesktop → 15f70f9、FlopsWeb cocoder-ui-core → 1f05e00、flops-chat-ui → 1b0fab3 (md 预览本地图片加载)'

seen_gitlink=0
problems=""
while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
    set -- ${meta#:}
    new_mode="$2"; new_sha="$4"
    [ "$new_mode" = "160000" ] || continue
    case "$new_sha" in *[!0]*) ;; *) continue ;; esac   # 新 SHA 全 0 = 删除子模块，豁免
    seen_gitlink=1
    name="${path##*/}"
    # 名字后紧跟「→ 短SHA」（只允许隔空白），保证每个子模块各有自己的箭头对，
    # 而不是几个名字共用一个；写出的短SHA必须是该子模块新指针的前缀。
    if [[ "$title" =~ (^|[^[:alnum:]_.-])"$name"[[:space:]]*→[[:space:]]*([0-9a-f]{7,40}) ]]; then
        short="${BASH_REMATCH[2]}"
        if [[ "$new_sha" != "$short"* ]]; then
            # 变量后紧跟全角字符必须加花括号：bash 3.2 会把多字节字符的首字节并进变量名
            problems+="  · ${name}：写的是 ${short}，新指针实际是 ${new_sha:0:7}"$'\n'
        fi
    else
        problems+="  · ${name}：标题缺少「$name → ${new_sha:0:7}」"$'\n'
    fi
done < <(gitlink_changes)

if [ "$seen_gitlink" = 0 ]; then
    {
        echo "✗ 标题声称 bump，但本次提交没有子模块指针（gitlink）变动。"
        echo "  纯版本号等非子模块更新请换 chore(release): 之类措辞。"
        echo "  紧急情况可用 git commit --no-verify 跳过（不推荐）。"
    } >&2
    exit 1
fi

if ! [[ "$title" =~ [(（].+[)）][[:space:]]*$ ]]; then
    problems+="  · 标题末尾缺少括号说明（写本次子仓更新了什么，多个子模块共用一份）"$'\n'
fi

if [ -n "$problems" ]; then
    {
        echo "✗ bump 提交的标题要让人不看 diff 就知道每个指针落到哪："
        printf '%s' "$problems"
        echo "  合格示例：$example"
        echo "  紧急情况可用 git commit --no-verify 跳过（不推荐）。"
    } >&2
    exit 1
fi

exit 0
