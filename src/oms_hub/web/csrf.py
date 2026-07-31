from fastapi import HTTPException, Request


def require_form_csrf(request: Request, submitted_token: str | None) -> None:
    protector = request.app.state.csrf
    token = submitted_token or request.headers.get(protector.header_name)
    if not protector.verify(
        request.cookies.get(protector.cookie_name),
        token,
    ):
        raise HTTPException(403, "request verification failed")
