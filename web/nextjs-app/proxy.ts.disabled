import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PROTECTED = ["/research", "/trace", "/memory", "/operator"];

export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (!PROTECTED.some((p) => pathname === p || pathname.startsWith(p + "/"))) {
    return NextResponse.next();
  }
  const token = request.cookies.get("finsight_token")?.value;
  if (!token) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("redirect", pathname);
    return NextResponse.redirect(loginUrl);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/research/:path*", "/trace/:path*", "/memory/:path*", "/operator/:path*"],
};
