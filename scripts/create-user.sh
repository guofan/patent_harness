#!/bin/bash
#
# 创建 DeerFlow 用户脚本
#
# 用法:
#   ./scripts/create-user.sh <邮箱> <密码> [姓名]
#
# 示例:
#   ./scripts/create-user.sh admin@company.com securepassword "管理员"
#   ./scripts/create-user.sh user@company.com userpassword
#

set -e

# 默认配置
API_URL="${DEERFLOW_API_URL:-http://localhost:2026}"

# 显示帮助
show_help() {
    echo "用法: $0 <邮箱> <密码> [姓名]"
    echo ""
    echo "示例:"
    echo "  $0 admin@company.com securepassword \"管理员\""
    echo "  $0 user@company.com userpassword"
    echo ""
    echo "环境变量:"
    echo "  DEERFLOW_API_URL - API 地址 (默认: http://localhost:2026)"
    exit 1
}

# 参数检查
if [ $# -lt 2 ]; then
    show_help
fi

EMAIL="$1"
PASSWORD="$2"
NAME="${3:-$1}"  # 如果没有提供姓名，使用邮箱前缀

# 验证邮箱格式
if [[ ! "$EMAIL" =~ ^[^@]+@[^@]+$ ]]; then
    echo "❌ 无效的邮箱格式: $EMAIL"
    exit 1
fi

# 验证密码长度
if [ ${#PASSWORD} -lt 6 ]; then
    echo "❌ 密码长度至少 6 位"
    exit 1
fi

echo "创建用户: $EMAIL"
echo "API地址: $API_URL"
echo ""

# 调用 Better Auth 注册 API
RESPONSE=$(curl -s -X POST "$API_URL/api/auth/sign-up/email" \
    -H "Content-Type: application/json" \
    -d "{
        \"email\": \"$EMAIL\",
        \"password\": \"$PASSWORD\",
        \"name\": \"$NAME\"
    }" 2>/dev/null || echo '{"error":"Connection failed"}')

# 检查响应
if echo "$RESPONSE" | grep -q '"error"'; then
    ERROR_MSG=$(echo "$RESPONSE" | grep -o '"message":"[^"]*"' | cut -d'"' -f4)
    if [ -z "$ERROR_MSG" ]; then
        ERROR_MSG="无法连接到服务器，请确保 DeerFlow 已启动 ($API_URL)"
    fi
    echo "❌ 创建失败: $ERROR_MSG"
    exit 1
else
    echo "✅ 用户创建成功"
    echo "   邮箱: $EMAIL"
    echo "   姓名: $NAME"
    echo ""
    echo "用户现在可以通过 $API_URL/login 登录"
fi
