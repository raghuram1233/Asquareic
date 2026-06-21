import tkinter as tk
from tkinter import filedialog, messagebox

from OCC.Display.backend import load_backend
load_backend("tk")

from OCC.Display.SimpleGui import init_display

from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.IFSelect import IFSelect_RetDone


class StepViewer:

    def __init__(self, root):

        self.root = root
        self.root.title("STEP File Viewer")
        self.root.geometry("1200x800")

        # Initialize OCC Display
        self.display, self.start_display, _, _ = init_display()

        # Button Frame
        top_frame = tk.Frame(root)
        top_frame.pack(side=tk.TOP, fill=tk.X)

        # Load Button
        load_btn = tk.Button(
            top_frame,
            text="Open STEP File",
            command=self.open_step_file,
            font=("Arial", 12)
        )
        load_btn.pack(pady=10)

    def load_step_shape(self, filepath):

        reader = STEPControl_Reader()

        status = reader.ReadFile(filepath)

        if status != IFSelect_RetDone:
            raise Exception("Failed to load STEP file")

        reader.TransferRoots()

        shape = reader.OneShape()

        return shape

    def open_step_file(self):

        filepath = filedialog.askopenfilename(
            title="Select STEP File",
            filetypes=[
                ("STEP Files", "*.step *.stp")
            ]
        )

        if not filepath:
            return

        try:
            shape = self.load_step_shape(filepath)

            self.display.EraseAll()
            self.display.DisplayShape(shape, update=True)
            self.display.FitAll()

            messagebox.showinfo(
                "Success",
                "STEP file loaded successfully"
            )

        except Exception as e:
            messagebox.showerror(
                "Error",
                str(e)
            )


if __name__ == "__main__":

    root = tk.Tk()

    viewer = StepViewer(root)

    viewer.start_display()