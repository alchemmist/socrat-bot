import os

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("socrat_bot.app:api", host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
