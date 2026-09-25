import { redirect } from "next/navigation";

/**
 * Onboarding now lives on Home, which walks a new user through the resume,
 * the interview and the first search in place. Old links land there.
 */
export default function OnboardingPage() {
  redirect("/home");
}
