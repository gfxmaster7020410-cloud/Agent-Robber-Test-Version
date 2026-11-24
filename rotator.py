class Other:
    def ping(self):
        return 1

class APIKeyRotator:
    def __init__(self, key: str, timeout: int = 30) -> None:
        self.key = key
        self.timeout = timeout

    def rotate(self) -> str:
        return self.key

def helper():
    return APIKeyRotator("x")
