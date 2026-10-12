import { NextResponse, type NextRequest } from "next/server";

/**
 * An optimistic redirect, not an authorization check.
 *
 * This only looks for the presence of Django's session cookie so signed-out visitors land on
 * /sign-in instead of a flash of empty app chrome. It cannot and does not validate the session:
 * every page reads Django as the visitor (`src/lib/api/server.ts`), and Django checks the session
 * on every call.
 *
 * It also passes the page's path and query on as `x-foray-path`, so that when Django answers 401
 * `django()` can send the visitor to `/sign-in?next=<where they were>`.
 */
export function proxy(request: NextRequest) {
  if (!request.cookies.has("sessionid")) {
    const signIn = new URL("/sign-in", request.url);
    signIn.searchParams.set("next", request.nextUrl.pathname);
    return NextResponse.redirect(signIn);
  }

  const headers = new Headers(request.headers);
  headers.set("x-foray-path", request.nextUrl.pathname + request.nextUrl.search);
  return NextResponse.next({ request: { headers } });
}

export const config = {
  matcher: [
    "/home/:path*",
    "/matches/:path*",
    "/chat/:path*",
    "/onboarding/:path*",
    "/activity/:path*",
    "/profile/:path*",
    "/companies/:path*",
    "/settings/:path*",
  ],
};
