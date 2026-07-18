import subprocess
import sys
import time

def main():
    print("Starting backend and frontend...")
    
    # Start backend process using module execution (-m) to preserve sys.path
    
    #backend = subprocess.Popen([sys.executable, "-m", "backend.main"])
    
    # Optional: wait a moment to ensure backend starts before frontend
    #time.sleep(1)
    
    # Start frontend process using module execution (-m)
    # frontend = subprocess.Popen([sys.executable, "-m", "frontend.overlay"])
    
    # try:
    #     # Keep the main process alive while the sub-processes are running
    #     backend.wait()
    #     frontend.wait()
    # except KeyboardInterrupt:
    #     print("\nStopping processes...")
    #     backend.terminate()
    #     frontend.terminate()
    #     backend.wait()
    #     frontend.wait()
    #     print("Processes stopped.")

if __name__ == "__main__":
    main()
