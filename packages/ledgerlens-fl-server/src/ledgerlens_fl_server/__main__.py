import uvicorn
from .config import settings

def main():
    uvicorn.run(
        "ledgerlens_fl_server.server:federated_app",
        host=settings.federated_server_host,
        port=settings.federated_server_port,
        reload=False
    )

if __name__ == "__main__":
    main()
