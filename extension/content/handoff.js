/*
 * The three things Foray hands back to the user and never does itself: a CAPTCHA, signing in,
 * and making an account. Finding one changes nothing on the page. It only decides what the
 * panel says, and it stops the extension pressing submit even when Foray would allow it.
 *
 * A CAPTCHA here means one that is asking for something: a challenge or a tick box on screen.
 * Greenhouse, Lever and Ashby all load an invisible one with every form, which only watches;
 * its frames are hidden or are the small corner badge, and that is not a hand-off.
 */
(() => {
  const Foray = (globalThis.Foray ??= {});
  if (Foray.handoff) return;
  const { text: T, dom: D } = Foray;

  const CAPTCHA_FRAME =
    /recaptcha\/(api2|enterprise)\/(anchor|bframe)|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs\.com|funcaptcha\.com|geetest\.com|captcha/i;
  const CREATING =
    /\b(create (an |your |my )?account|sign up|register|new password|confirm (your )?password|re enter password|verify password)\b/;

  const SAY = {
    captcha:
      "This page is asking for a CAPTCHA. Foray never solves those: complete it yourself, then press submit.",
    login:
      "This page asks you to sign in. Foray never signs in for you. Sign in yourself; if the form appears after that, press Fill the form again.",
    account:
      "This page asks you to create an account. Foray never makes accounts. If you want to go on, create it yourself, then press Fill the form again.",
  };

  function captcha() {
    for (const frame of document.querySelectorAll("iframe")) {
      const about = `${frame.getAttribute("src") ?? ""} ${frame.getAttribute("title") ?? ""}`;
      if (!CAPTCHA_FRAME.test(about) || !D.visible(frame)) continue;
      const box = frame.getBoundingClientRect();
      if (box.width < 40 || box.height < 40) continue;
      // reCAPTCHA's corner badge says the page is watched. It asks for nothing.
      if (frame.closest(".grecaptcha-badge")) continue;
      return frame;
    }
    return null;
  }

  function signIn() {
    const passwords = [...document.querySelectorAll('input[type="password"]')].filter(D.visible);
    if (!passwords.length) return null;
    const scope = passwords[0].closest("form") ?? document.body;
    const creating = passwords.length > 1 || CREATING.test(T.norm(scope.textContent).slice(0, 6000));
    return { kind: creating ? "account" : "login", element: passwords[0] };
  }

  /** `{kind, message, element}` for the first hand-off on the page, else null. */
  function detect() {
    const frame = captcha();
    if (frame) return { kind: "captcha", message: SAY.captcha, element: frame };
    const asked = signIn();
    if (asked) return { ...asked, message: SAY[asked.kind] };
    return null;
  }

  Foray.handoff = { detect };
})();
