import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// 移除认证检查，所有路径直接放行
export async function middleware(request: NextRequest) {
  return NextResponse.next();
}

// 保留 matcher 配置以避免 Next.js 警告
export const config = {
  matcher: [],
};
