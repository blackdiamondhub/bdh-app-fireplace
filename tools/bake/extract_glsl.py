"""Extract the flame GLSL from the hass-napoleon card so the baker uses it verbatim."""
import pathlib, re, sys
src = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
frag = re.search(r"const FRAG = `(.*?)`;", src, re.S).group(1)
start = frag.index("// small-magnitude hash")
end = frag.index("vec3 flameColor")
pathlib.Path(sys.argv[2]).write_text(frag[start:end].strip() + "\n", encoding="utf-8")
print("ok", end - start)
