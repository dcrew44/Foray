"""Resume and document endpoints (design §5.1, §5.3). Stub routers with their final prefixes;
later steps add uploads, document downloads, the base resume (PUT /resume, POST /resume/render),
previews and POST /resumes/tailor."""

from apps.core.router import router

uploads = router(tags=["uploads"])
documents = router(tags=["documents"])
resume = router(tags=["resume"])
resumes = router(tags=["resumes"])

ROUTERS = {"/uploads": uploads, "/documents": documents, "/resume": resume, "/resumes": resumes}
