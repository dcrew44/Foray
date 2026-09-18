import { getSessionCookie } from "better-auth/cookies";
import { NextResponse, type NextRequest } from "next/server";

/**
 * An optimistic redirect, not an authorization check.
 *
 * This only looks for the presence of a session cookie so signed-out visitors
 * land on /sign-in instead of a flash of empty app chrome. It cannot and does
 * not validate the session — every page and action still calls `requireUser()`,
 * which verifies against the database.
 */
export function proxy(request: NextRequest) {
  if (getSessionCookie(request)) return NextResponse.next();

  const signIn = new URL("/sign-in", request.url);
  signIn.searchParams.set("next", request.nextUrl.pathname);
  return NextResponse.redirect(signIn);
}

export const config = {
  matcher: [
    "/chat/:path*",
    "/onboarding/:path*",
    "/pipeline/:path*",
    "/jobs/:path*",
    "/activity/:path*",
    "/profile/:path*",
    "/companies/:path*",
    "/settings/:path*",
  ],
};
