"""mock-finance：本阶段只有健康检查，后续阶段接入真正的财务接口时再实现。"""
import uvicorn
from fastapi import FastAPI

app = FastAPI(title="mock-finance")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
