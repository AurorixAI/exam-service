"""Exam Service — Custom exception hierarchy"""


class ExamServiceError(Exception):
    def __init__(self, message: str, code: str = "EXAM_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class ExamNotFoundError(ExamServiceError):
    def __init__(self, exam_id: str):
        super().__init__(f"Exam {exam_id} not found", "EXAM_NOT_FOUND")


class ExamNotActiveError(ExamServiceError):
    def __init__(self, exam_id: str):
        super().__init__(f"Exam {exam_id} is not active", "EXAM_NOT_ACTIVE")


class SessionNotFoundError(ExamServiceError):
    def __init__(self, session_id: str):
        super().__init__(f"Session {session_id} not found", "SESSION_NOT_FOUND")


class SessionAlreadySubmittedError(ExamServiceError):
    def __init__(self, session_id: str):
        super().__init__(
            f"Session {session_id} already submitted", "SESSION_ALREADY_SUBMITTED"
        )


class NoTasksAvailableError(ExamServiceError):
    def __init__(self, detail: str):
        super().__init__(detail, "NO_TASKS_AVAILABLE")


class InvalidExamConfigError(ExamServiceError):
    def __init__(self, detail: str):
        super().__init__(detail, "INVALID_EXAM_CONFIG")
