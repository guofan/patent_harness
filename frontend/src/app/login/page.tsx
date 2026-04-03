"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function LoginPage() {
  const router = useRouter();

  useEffect(() => {
    // 自动跳转到首页，无需登录
    router.push("/");
  }, [router]);

  return (
    <div className="flex h-screen items-center justify-center">
      <p>正在跳转...</p>
    </div>
  );
}
