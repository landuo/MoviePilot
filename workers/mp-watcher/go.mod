module github.com/jxxghp/MoviePilot/workers/mp-watcher

go 1.22

require (
	github.com/fsnotify/fsnotify v1.7.0
	github.com/jxxghp/MoviePilot/workers/shared v0.0.0
)

require golang.org/x/sys v0.4.0 // indirect

replace github.com/jxxghp/MoviePilot/workers/shared => ../shared
