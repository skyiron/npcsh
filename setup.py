from setuptools import setup, find_packages
import os


def package_files(directory):
    paths = []
    for path, directories, filenames in os.walk(directory):
        for filename in filenames:
            paths.append(os.path.join(path, filename))
    return paths


extra_files = package_files("npcsh/npc_profiles")

setup(
    name="npcsh",
    version="0.1.30",
    packages=find_packages(exclude=["tests*"]),
    install_requires=[
        "anthropic",
        "sentence_transformers",
        "nltk",
        "pypdf",
        "PyMuPDF",
        "opencv-python",
        "librosa",
        "openai",
        "jinja2",
        "pandas",
        "matplotlib", 
        "IPython",
        "ollama",
        "requests",
        "markdown",
        "PyYAML",
        "langchain",
        "langchain_community",
        "openai-whisper",
        "pyaudio",
        "pygments",
        "pyttsx3",
        "chromadb",
        "gtts",
        "playsound",
        "termcolor",
        "colorama",
        "python-dotenv",
        "pytest",
    ],
    entry_points={
        "console_scripts": [
            "npcsh=npcsh.npcsh:main",
        ],
    },
    author="Christopher Agostino",
    author_email="cjp.agostino@example.com",
    description="npcsh is a command line tool for integrating LLMs into everyday workflows",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/cagostino/npcsh",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
    ],
    include_package_data=True,
    data_files=[("npcsh/npc_profiles", extra_files)],
    python_requires=">=3.10",
)
