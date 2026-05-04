import asyncio
import httpx
import time

async def main():
    async with httpx.AsyncClient() as client:
        # 1. Start a chat run that takes a while
        print("Starting task...")
        res = await client.post("http://localhost:8000/run", json={
            "query": "Write a long story about a slow turtle, counting from 1 to 100 with a lot of detail.",
            "model": "google_genai:gemini-2.0-flash",
            "conversation_id": None,
            "attachments": []
        })
        data = res.json()
        task_id = data["task_id"]
        print(f"Started task {task_id}")
        
        # 2. Wait a bit
        await asyncio.sleep(2.0)
        
        # 3. List tasks
        res = await client.get("http://localhost:8000/task-runs")
        print(f"Tasks before stop: {res.json()}")
        
        # 4. Stop the task
        print("Stopping task...")
        res = await client.post(f"http://localhost:8000/task-runs/{task_id}/stop")
        print(f"Stop response: {res.json()}")
        
        # 5. List tasks again
        await asyncio.sleep(1.0)
        res = await client.get("http://localhost:8000/task-runs")
        print(f"Tasks after stop: {res.json()}")

if __name__ == "__main__":
    asyncio.run(main())
