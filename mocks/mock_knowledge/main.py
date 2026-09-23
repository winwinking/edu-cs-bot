"""mock-knowledge：本阶段只有健康检查，阶段二做检索接口时再实现真正的行为。"""
import uvicorn
from fastapi import FastAPI

app = FastAPI(title="mock-knowledge")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
