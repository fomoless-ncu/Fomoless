# SAM 在 Linux 建置容器內執行此 target，名稱必須對應 template.yaml 的 Layer logical ID。

# SAM 要呼叫的建置目標，對應 template.yaml 的 SharedPythonLayer
.PHONY: build-SharedPythonLayer  
# 告訴 Make 這是工作名稱，不是要檢查是否存在的同名檔案
build-SharedPythonLayer: 
	# 由 SAM 指定的輸出目錄，建置時會換成實際路徑
	python3 build_layer.py "$(ARTIFACTS_DIR)"  