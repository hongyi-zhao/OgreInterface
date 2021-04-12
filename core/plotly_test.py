import plotly.graph_objects as go
import numpy as np

#  X, Y, Z = np.mgrid[-8:8:40j, -8:8:40j, -8:8:40j]
X, Y, Z = np.meshgrid(
    np.linspace(-5,5,10),
    np.linspace(-5,5,10),
    np.linspace(-5,5,10),
)
values = X**2 + Y**2 + Z**2


N = 40
R = 1
x,y,z = np.meshgrid(np.linspace(-R, R, N), np.linspace(-R, R, N),np.linspace(-R, R, N))
x,y,z = np.meshgrid(np.linspace(-R, R, N), np.linspace(-R, R, N),np.linspace(-R, R, N))

# filter points outside ellipsoid interior:
mask = (2*x)**2 + (3*y)**2 + z**2 <= R**2
x = x[mask]
y = y[mask]
z = z[mask]

fig = go.Figure(data=go.Volume(
    x=X.flatten(),
    y=Y.flatten(),
    z=Z.flatten(),
    value=values.flatten(),
    isomin=0.1,
    isomax=0.8,
    opacity=0.1, # needs to be small to see through all surfaces
    surface_count=17, # needs to be a large number for good volume rendering
    ))
fig.show()
