from spectrograms import app

if __name__ == "__main__":
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(use_reloader = True, debug = False)
