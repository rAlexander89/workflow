"""View models: the state and commands each screen binds to.

MVVM here means: the Model is the application (use cases and the domain), the View is a Jinja template,
and the ViewModel sits between them. A view model asks the use cases for what its screen shows, shapes it
for display, and exposes the actions the screen can take. A template reads only its view model, and a route
only creates one and renders it.
"""
