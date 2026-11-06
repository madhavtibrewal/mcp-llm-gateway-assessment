"""Create a token for the gateway:  uv run python -m task2_mcp_gateway.mint_token --role viewer"""

import argparse
import time

import jwt

from .gateway import JWT_SECRET


def mint(role: str, subject: str = "dev-user", days: float = 365) -> str:
    now = int(time.time())
    return jwt.encode({"sub": subject, "role": role, "iat": now, "exp": now + int(days * 86400)}, JWT_SECRET, algorithm="HS256")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", required=True)
    parser.add_argument("--sub", default="dev-user")
    parser.add_argument("--days", type=float, default=365)
    args = parser.parse_args()
    print(mint(args.role, args.sub, args.days))
